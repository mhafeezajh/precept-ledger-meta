"""Ledger HTTP API (ARCHITECTURE "API"): append, records, proofs, checkpoints, export.

Fail closed (R3, R4): a 2xx append response always carries a signed receipt, and every error
is a JSON body ``{"error": {"code", "message"}}`` with a status chosen from its code. An
unexpected exception is a bare 500 with no detail. Producers authenticate by signing their
records; reads are unauthenticated in the demo (see docs/modules/api.md, known gaps).
"""

import asyncio
import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from common.errors import (
    FormatError,
    LedgerError,
    MerkleError,
    PreceptError,
    SignatureError,
    StoreError,
)
from common.logging import accept_trace_id, log_event, trace_id_var
from ledger.checkpoint import Checkpoint, NoteKey, NoteSigner, open_checkpoint, peek_checkpoint
from ledger.checkpoint_loop import CheckpointLoop
from ledger.checkpointer import Checkpointer, CheckpointSink
from ledger.export import export_bundle
from ledger.format import MAX_RECORD_BYTES, b64encode
from ledger.service import Ledger
from ledger.store import Store
from merkle.log import LogTree
from merkle.store import TreeStore

logger = logging.getLogger("pl.ledger.api")


@dataclass(frozen=True)
class StreamLog:
    """One stream's Merkle tree, checkpoint store and checkpointer."""

    stream: str
    origin: str
    tree: LogTree
    sink: CheckpointSink  # where checkpoints are read back from: the checkpointer's first sink
    checkpointer: Checkpointer
    key: NoteKey

    @classmethod
    def create(
        cls,
        stream: str,
        origin: str,
        tree_store: TreeStore,
        sinks: Sequence[CheckpointSink],
        signer: NoteSigner,
    ) -> "StreamLog":
        tree = LogTree(tree_store)
        checkpointer = Checkpointer(tree, signer, origin, sinks)
        return cls(
            stream, origin, tree, sinks[0], checkpointer, NoteKey(origin, signer.public_key_raw)
        )


@dataclass(frozen=True)
class Services:
    ledger: Ledger
    store: Store
    logs: Mapping[str, StreamLog]


type ServicesFactory = Callable[[], AbstractAsyncContextManager[Services]]


class RequestError(PreceptError):
    """A request problem that has its own HTTP status."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message, code=code)
        self.status = status


_STATUS_BY_CODE = {
    "ledger.not_found": 404,
    "ledger.unknown_stream": 404,
    "checkpoint.not_found": 404,
    "ledger.duplicate_record": 409,
    "proof.not_checkpointed": 409,
    "format.too_large": 413,
    # The ledger cannot vouch for its own state: a server fault, never a client one.
    "checkpoint.inconsistent": 503,
    "checkpoint.conflict": 503,
    "checkpoint.unverifiable": 503,
}


def status_for(exc: PreceptError) -> int:
    if isinstance(exc, RequestError):
        return exc.status
    if exc.code in _STATUS_BY_CODE:
        return _STATUS_BY_CODE[exc.code]
    if isinstance(exc, StoreError):
        return 503
    if isinstance(exc, SignatureError):
        return 403
    if isinstance(exc, FormatError | MerkleError | LedgerError):
        return 400
    return 500


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def _stream_log(services: Services, stream: str | None) -> StreamLog:
    if stream is None:
        if len(services.logs) != 1:
            raise RequestError("stream is required", code="request.stream_required", status=400)
        return next(iter(services.logs.values()))
    log = services.logs.get(stream)
    if log is None:
        raise LedgerError(f"stream {stream!r} is not served here", code="ledger.unknown_stream")
    return log


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise FormatError(f"request exceeds {limit} bytes", code="format.too_large")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise FormatError(f"request exceeds {limit} bytes", code="format.too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _checkpoint_at(log: StreamLog, size: int | None) -> tuple[Checkpoint, str]:
    notes = await log.sink.notes()
    if size is not None:
        notes = [note for note in notes if peek_checkpoint(note).size == size]
    if not notes:
        raise LedgerError("no such checkpoint", code="checkpoint.not_found")
    note = notes[-1]
    try:
        # Never serve a checkpoint our own key does not verify (R5).
        return open_checkpoint(note, log.key), note
    except LedgerError as exc:
        raise LedgerError(
            "stored checkpoint does not verify", code="checkpoint.unverifiable"
        ) from exc


router = APIRouter()


@router.post("/ledger/v1/streams/{stream:path}/records", status_code=201)
async def append_record(stream: str, request: Request) -> JSONResponse:
    services = _services(request)
    _stream_log(services, stream)
    media_type = request.headers.get("content-type", "").split(";")[0].strip()
    if media_type != "application/json":
        raise RequestError(
            "content type must be application/json", code="request.media_type", status=415
        )
    receipt = await services.ledger.append_json(
        stream, await _read_limited(request, MAX_RECORD_BYTES)
    )
    return JSONResponse(receipt.to_json(), status_code=201)


@router.get("/ledger/v1/records/{record_id}")
async def get_record(record_id: str, request: Request) -> dict[str, Any]:
    record, receipt = await _services(request).ledger.get(record_id)
    return {"record": record.to_json(), "receipt": receipt.to_json()}


@router.get("/ledger/v1/records/{record_id}/proof")
async def record_proof(
    record_id: str, request: Request, size: Annotated[int | None, Query(ge=0)] = None
) -> dict[str, Any]:
    services = _services(request)
    _, receipt = await services.ledger.get(record_id)
    log = _stream_log(services, receipt.stream)
    checkpoint, note = await _checkpoint_at(log, size)
    if receipt.seq > checkpoint.size:
        raise LedgerError(
            f"seq {receipt.seq} is after checkpoint {checkpoint.size}",
            code="proof.not_checkpointed",
        )
    head = await log.tree.head(checkpoint.size)
    if not hmac.compare_digest(head.root, checkpoint.root):
        raise LedgerError(
            "tree no longer matches its signed checkpoint", code="checkpoint.inconsistent"
        )
    proof = await log.tree.prove_inclusion(receipt.seq - 1, checkpoint.size)
    return {
        "record_id": record_id,
        "seq": receipt.seq,
        "leaf_index": proof.index,
        "tree_size": proof.size,
        "leaf_hash": receipt.leaf_hash,
        "hashes": [b64encode(h) for h in proof.hashes],
        "checkpoint": note,
    }


@router.get("/ledger/v1/checkpoints/latest")
async def latest_checkpoint(request: Request, stream: str | None = None) -> dict[str, Any]:
    log = _stream_log(_services(request), stream)
    checkpoint, note = await _checkpoint_at(log, None)
    return {
        "stream": log.stream,
        "size": checkpoint.size,
        "root": b64encode(checkpoint.root),
        "note": note,
        "cosignatures": [],  # witness/ (day 3)
        "anchors": [],  # anchor/ (day 4)
    }


@router.get("/ledger/v1/consistency")
async def consistency(
    request: Request,
    from_: Annotated[int, Query(alias="from", ge=0)],
    to: Annotated[int, Query(ge=0)],
    stream: str | None = None,
) -> dict[str, Any]:
    log = _stream_log(_services(request), stream)
    proof = await log.tree.prove_consistency(from_, to)
    return {"stream": log.stream, "from": from_, "to": to,
            "hashes": [b64encode(h) for h in proof.hashes]}  # fmt: skip


@router.get("/ledger/v1/export")
async def export(
    request: Request,
    stream: str | None = None,
    from_: Annotated[int | None, Query(alias="from")] = None,
    to: int | None = None,
) -> Response:
    if from_ is not None or to is not None:
        raise RequestError(
            "range exports are not supported yet; omit from and to",
            code="export.range_unsupported",
            status=400,
        )
    services = _services(request)
    log = _stream_log(services, stream)
    bundle = await export_bundle(services.store, log.sink, log.stream, log.origin)
    filename = log.stream.replace("/", "_") + ".tar"
    return Response(
        bundle,
        media_type="application/x-tar",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ledger/v1/health")
async def health(request: Request) -> JSONResponse:
    loop: CheckpointLoop | None = request.app.state.checkpoint_loop
    body: dict[str, Any] = {
        "status": "ok",
        "checkpoint_loop": "disabled" if loop is None else loop.status(),
        "streams": sorted(_services(request).logs),
    }
    if loop is not None:
        body["checkpoint_errors"] = loop.last_errors()
        if loop.status() == "stopped":
            body["status"] = "failing"
    return JSONResponse(body, status_code=503 if body["status"] == "failing" else 200)


def _register_handlers(app: FastAPI) -> None:
    @app.exception_handler(PreceptError)
    async def precept_error(request: Request, exc: PreceptError) -> JSONResponse:
        return _error(status_for(exc), exc.code, str(exc.args[0]))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default body echoes the input; ours does not.
        return _error(422, "request.invalid", "invalid request parameters")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log_event(logger, logging.ERROR, "unhandled error", error=type(exc).__name__)
        return _error(500, "internal", "internal error")

    @app.middleware("http")
    async def trace(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = trace_id_var.set(accept_trace_id(request.headers.get("x-trace-id")))
        started = time.perf_counter()
        try:
            response = await call_next(request)
            route = request.scope.get("route")
            log_event(
                logger,
                logging.INFO,
                "request",
                method=request.method,
                route=getattr(route, "path", "unmatched"),  # template, not the raw path
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            response.headers["x-trace-id"] = trace_id_var.get()
            return response
        finally:
            trace_id_var.reset(token)


def create_app(open_services: ServicesFactory, *, checkpoint_interval: float | None) -> FastAPI:
    """``open_services`` is entered for the app's lifetime; the checkpoint loop runs inside it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_services() as services:
            app.state.services = services
            app.state.checkpoint_loop = None
            task = None
            if checkpoint_interval is not None:
                loop = CheckpointLoop(
                    {s: log.checkpointer for s, log in services.logs.items()}, checkpoint_interval
                )
                app.state.checkpoint_loop = loop
                task = asyncio.create_task(loop.run_forever())
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Precept Ledger", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    _register_handlers(app)
    return app

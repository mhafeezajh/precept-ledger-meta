"""Witness HTTP API: ``POST /witness/v1/cosign`` and the C2SP path ``/witness/v1/add-checkpoint``.

Request and response bodies follow C2SP tlog-witness: the request is ``old <size>``, the
proof hashes, a blank line and the checkpoint note; success is ``200`` with the cosignature
line; a wrong old size is ``409`` with ``text/x.tlog.size`` carrying the size this witness
last cosigned. Other refusals use the JSON error body shared with the ledger API (D35), with
the statuses tlog-witness assigns (400, 403, 404, 422).
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from common.errors import PreceptError, WitnessError
from common.http import error_response, install_common_handlers, read_limited
from ledger.cosignature import MAX_REQUEST_BYTES, parse_add_checkpoint
from witness.core import Witness

logger = logging.getLogger("pl.witness.api")

_STATUS_BY_CODE = {
    "witness.bad_request": 400,
    "witness.bad_old_size": 400,
    "request.origin_required": 400,
    "witness.bad_signature": 403,
    "witness.unknown_log": 404,
    "witness.not_found": 404,
    "witness.conflict": 409,
    "format.too_large": 413,
    "witness.inconsistent": 422,
    "witness.state_invalid": 503,  # cannot vouch for its own state: refuse everything
}


def _witness(request: Request) -> Witness:
    witness: Witness = request.app.state.witness
    return witness


def _origin(witness: Witness, origin: str | None) -> str:
    if origin is not None:
        return origin
    if len(witness.origins) != 1:
        raise WitnessError("origin is required", code="request.origin_required")
    return witness.origins[0]


async def _cosign(request: Request) -> Response:
    old_size, proof, note = parse_add_checkpoint(await read_limited(request, MAX_REQUEST_BYTES))
    try:
        line = await _witness(request).add_checkpoint(old_size, proof, note)
    except WitnessError as exc:
        if exc.code == "witness.conflict" and exc.latest_size is not None:
            # Exactly this content type (C2SP tlog-witness); media_type= would add a charset.
            body = f"{exc.latest_size}\n".encode()
            return Response(body, 409, headers={"content-type": "text/x.tlog.size"})
        raise
    return PlainTextResponse(line)


async def _latest(request: Request, origin: str | None = None) -> Response:
    witness = _witness(request)
    origin = _origin(witness, origin)
    if origin not in witness.origins:
        raise WitnessError(f"unknown log {origin!r}", code="witness.unknown_log")
    note = await witness.latest_note(origin)
    if note is None:
        raise WitnessError("nothing cosigned for this log yet", code="witness.not_found")
    return PlainTextResponse(note)


async def _health(request: Request) -> JSONResponse:
    witness = _witness(request)
    body: dict[str, Any] = {
        "status": "ok",
        "name": witness.name,
        "cosigner_key": witness.key.verifier(),
        "logs": [],
    }
    for origin in witness.origins:
        try:
            latest = await witness.latest(origin)
        except PreceptError as exc:
            body["status"] = "failing"
            body["logs"].append({"origin": origin, "error": exc.code})
            continue
        body["logs"].append({"origin": origin, "size": None if latest is None else latest.size})
    return JSONResponse(body, status_code=503 if body["status"] == "failing" else 200)


def create_witness_app(witness: Witness) -> FastAPI:
    app = FastAPI(title="Precept Ledger witness", version="0.1.0")
    app.state.witness = witness
    for path in ("/witness/v1/cosign", "/witness/v1/add-checkpoint"):
        app.add_api_route(path, _cosign, methods=["POST"])
    app.add_api_route("/witness/v1/latest", _latest, methods=["GET"])
    app.add_api_route("/witness/v1/health", _health, methods=["GET"])

    @app.exception_handler(PreceptError)
    async def precept_error(request: Request, exc: PreceptError) -> JSONResponse:
        status = _STATUS_BY_CODE.get(exc.code, 500)
        return error_response(status, exc.code, str(exc.args[0]))

    install_common_handlers(app, logger)
    return app

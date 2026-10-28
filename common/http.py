"""HTTP plumbing shared by the ledger and witness APIs: error bodies, size-limited reads,
and one JSON log line per request with a ``trace_id``.

Every error body is ``{"error": {"code", "message"}}``. Validation errors and unexpected
exceptions never echo the input or the exception message (D35).
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from common.errors import FormatError
from common.logging import accept_trace_id, log_event, trace_id_var


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


async def read_limited(request: Request, limit: int) -> bytes:
    """The request body, refusing more than ``limit`` bytes with or without Content-Length."""
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


def install_common_handlers(app: FastAPI, logger: logging.Logger) -> None:
    """Validation and unexpected-error handlers, and the trace and request-log middleware."""

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default body echoes the input; ours does not.
        return error_response(422, "request.invalid", "invalid request parameters")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log_event(logger, logging.ERROR, "unhandled error", error=type(exc).__name__)
        return error_response(500, "internal", "internal error")

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

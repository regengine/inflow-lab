from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .mock_service import MockRegEngineHTTPError

# How much of an offending value to quote back. Enough to identify which field
# is wrong, nowhere near enough to echo a document.
_MAX_ECHOED_INPUT = 200


async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _truncate(value: Any) -> Any:
    """Shrink an error's echoed input to something bounded."""
    if isinstance(value, str) and len(value) > _MAX_ECHOED_INPUT:
        return f"{value[:_MAX_ECHOED_INPUT]}... ({len(value)} characters)"
    if isinstance(value, list) and len(value) > 10:
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    return value


async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422 without echoing the whole rejected body back.

    Pydantic puts the offending value in each error's `input`, and FastAPI
    serialises `exc.errors()` wholesale -- so rejecting an oversized field
    allocated and returned *more* than accepting it would have. Measured at 6x
    the request size for control characters, which turned every length cap
    into an amplifier pointing at the caller.

    The field location and message are what a caller needs; the value is what
    they already sent.
    """
    errors = []
    for error in exc.errors():
        trimmed = {key: value for key, value in error.items() if key not in {"input", "ctx", "url"}}
        if "input" in error:
            trimmed["input"] = _truncate(error["input"])
        errors.append(trimmed)
    return JSONResponse(status_code=422, content={"detail": errors})


async def handle_mock_http_error(_: Request, exc: MockRegEngineHTTPError) -> JSONResponse:
    """Let the stand-in answer with the status live RegEngine would.

    `MockRegEngineService` raises this with a real status and detail -- 422 for
    an oversized batch, 401/402/429 for the friction codes -- but nothing was
    registered for it, so the HTTP route turned every one into an opaque 500.
    That inverts the parity this simulator exists to demonstrate.
    """
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

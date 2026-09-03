from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})

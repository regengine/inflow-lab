from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import TenantContext, tenant_context_from_request
from .cors import cors_origins_from_env
from .dependencies import get_tenant_context
from .tenancy import active_controller_for_context


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
REQUEST_LOGGER = logging.getLogger("regengine.request")


async def auth_and_tenant_middleware(request: Request, call_next):
    started_at = time.perf_counter()
    response = None

    try:
        if request.method == "OPTIONS" or request.url.path == "/api/healthz":
            response = await call_next(request)
            return response

        context = tenant_context_from_request(request)
        if isinstance(context, JSONResponse):
            response = context
            return response
        request.state.tenant_context = context
        unsafe_origin_response = _reject_untrusted_unsafe_origin(request, context)
        if unsafe_origin_response is not None:
            response = unsafe_origin_response
            return response
        response = await call_next(request)
        return response
    except Exception:
        _log_request(request, status_code=500, started_at=started_at)
        raise
    finally:
        if response is not None:
            _log_request(request, status_code=response.status_code, started_at=started_at)


def _log_request(request: Request, status_code: int, started_at: float) -> None:
    duration_ms = (time.perf_counter() - started_at) * 1000
    context = get_tenant_context(request)
    delivery_mode = _request_delivery_mode(request)
    REQUEST_LOGGER.info(
        "request method=%s path=%s status=%s duration_ms=%.2f tenant=%s delivery_mode=%s",
        request.method,
        request.url.path,
        status_code,
        duration_ms,
        context.tenant_id,
        delivery_mode,
    )


def _request_delivery_mode(request: Request) -> str:
    if not hasattr(request.state, "tenant_context") and request.url.path != "/api/healthz":
        return "unknown"
    try:
        return str(active_controller_for_context(get_tenant_context(request)).config.delivery.mode.value)
    except Exception:
        return "unknown"


def _reject_untrusted_unsafe_origin(request: Request, context: TenantContext) -> JSONResponse | None:
    if not context.auth_enabled or request.method.upper() not in UNSAFE_METHODS:
        return None

    request_origin = _browser_request_origin(request)
    if request_origin is None:
        return None

    if request_origin in cors_origins_from_env():
        return None

    return JSONResponse(
        status_code=403,
        content={"detail": "State-changing requests require a trusted browser origin"},
    )


def _browser_request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return _origin_from_url(origin) or ""

    referer = request.headers.get("referer")
    if referer:
        return _origin_from_url(referer) or ""

    return None


def _origin_from_url(raw_url: str) -> str | None:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"

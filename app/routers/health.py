from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import tenancy
from ..auth import TenantContext
from ..build_info import current_build_info
from ..contract import INFLOW_CONTRACT_VERSION
from ..controller import SimulationController
from ..dependencies import get_active_controller, get_tenant_context


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Health"])


def _store_probe(store: Any) -> dict[str, Any]:
    """Run the store's cheap write probe, never raising at the caller."""
    try:
        probe = store.check_writable()
    except Exception as exc:  # pragma: no cover - defensive, probe handles OSError itself
        logger.error("health check store probe raised", exc_info=exc)
        return {
            "ok": False,
            "persist_path": str(getattr(store, "persist_path", "unknown")),
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not probe.get("ok"):
        logger.error(
            "health check failed: event store is not writable",
            extra={
                "persist_path": probe.get("persist_path"),
                "probe_error": probe.get("error"),
            },
        )
    return probe


@router.get("/health")
async def health(
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> dict[str, Any]:
    build = current_build_info().public_dict()
    store_probe = _store_probe(active_controller.store)
    return {
        "ok": bool(store_probe["ok"]),
        "utc_time": datetime.now(UTC).isoformat(),
        "build": build,
        "contract_version": INFLOW_CONTRACT_VERSION,
        "tenant": context.tenant_id,
        "auth": {
            "enabled": context.auth_enabled,
            "username": context.username,
            "uses_default_storage": context.uses_default_storage,
        },
        "store": store_probe,
        "status": active_controller.status(),
    }


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe for Railway and Docker.

    This is the endpoint wired into ``healthcheckPath`` and the Dockerfile
    ``HEALTHCHECK``, so it has to fail when the deployment cannot actually
    do its job. It checks that the default tenant's event store can still
    write to its persist path (a blank line appended and truncated away),
    which is the failure mode — full disk, detached volume, permission
    change — a health check exists to catch.
    """
    store_probe = _store_probe(tenancy.store)
    body: dict[str, Any] = {
        "ok": bool(store_probe["ok"]),
        "utc_time": datetime.now(UTC).isoformat(),
        "build": current_build_info().public_dict(),
        "contract_version": INFLOW_CONTRACT_VERSION,
        "store": store_probe,
    }
    return JSONResponse(content=body, status_code=200 if body["ok"] else 503)

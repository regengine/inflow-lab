from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import tenancy
from ..auth import TenantContext
from ..build_info import current_build_info
from ..contract import INFLOW_CONTRACT_VERSION
from ..controller import SimulationController
from ..dependencies import get_active_controller, get_tenant_context
from ..schemas.health import HealthResponse, HealthzResponse


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Health"])


@router.get("/health", response_model=HealthResponse)
async def health(
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> HealthResponse:
    store_probe = active_controller.store.check_writable()
    ok = bool(store_probe.get("ok"))
    build = current_build_info().public_dict()
    return HealthResponse(
        ok=ok,
        utc_time=datetime.now(UTC).isoformat(),
        build=build,
        contract_version=INFLOW_CONTRACT_VERSION,
        store=store_probe,
        tenant=context.tenant_id,
        auth={
            "enabled": context.auth_enabled,
            "username": context.username,
            "uses_default_storage": context.uses_default_storage,
        },
        status=active_controller.status(),
    )


@router.get("/healthz", response_model=HealthzResponse)
async def healthz() -> JSONResponse:
    store_probe = tenancy.store.check_writable()
    ok = bool(store_probe.get("ok"))
    if not ok:
        logger.error(
            "default event store is not writable",
            extra={
                "persist_path": store_probe.get("persist_path"),
                "error": store_probe.get("error"),
            },
        )
    body = HealthzResponse(
        ok=ok,
        utc_time=datetime.now(UTC).isoformat(),
        build=current_build_info().public_dict(),
        contract_version=INFLOW_CONTRACT_VERSION,
        store=store_probe,
    )
    return JSONResponse(status_code=200 if ok else 503, content=body.model_dump(mode="json"))

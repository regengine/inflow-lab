from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from ..auth import TenantContext
from ..build_info import current_build_info
from ..controller import SimulationController
from ..dependencies import get_active_controller, get_tenant_context


router = APIRouter(prefix="/api", tags=["Health"])


@router.get("/health")
async def health(
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> dict[str, Any]:
    build = current_build_info().public_dict()
    return {
        "ok": True,
        "utc_time": datetime.now(UTC).isoformat(),
        "build": build,
        "tenant": context.tenant_id,
        "auth": {
            "enabled": context.auth_enabled,
            "username": context.username,
            "uses_default_storage": context.uses_default_storage,
        },
        "status": active_controller.status(),
    }


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "utc_time": datetime.now(UTC).isoformat(),
        "build": current_build_info().public_dict(),
    }

from __future__ import annotations

import asyncio
import shutil

from fastapi import APIRouter, Depends

from .. import tenancy
from ..dependencies import require_operator_auth
from ..schemas.operator import TenantListResponse, TenantOperationResponse, TenantSummary

router = APIRouter(prefix="/api/operator", tags=["Operator"])


@router.get("/tenants", response_model=TenantListResponse)
async def list_operator_tenants(_: None = Depends(require_operator_auth)) -> TenantListResponse:
    # `tenant_summary` stats the tenant's JSONL and, for uncached tenants,
    # counts its lines -- blocking work that scales with the store and used to
    # run on the event loop once per tenant.
    tenant_ids = await asyncio.to_thread(tenancy.known_tenant_ids)
    summaries = await asyncio.to_thread(
        lambda: [tenancy.tenant_summary(tenant_id) for tenant_id in tenant_ids]
    )
    return TenantListResponse(
        tenants=[TenantSummary.model_validate(summary) for summary in summaries]
    )


@router.post("/tenants/{tenant_id}/reset", response_model=TenantOperationResponse)
async def reset_operator_tenant(
    tenant_id: str,
    _: None = Depends(require_operator_auth),
) -> TenantOperationResponse:
    normalized_tenant = tenancy.operator_tenant_id(tenant_id)
    tenant_controller = tenancy.get_tenant_controller_for_id(normalized_tenant)
    await tenant_controller.reset()
    return TenantOperationResponse(status="reset", tenant_id=normalized_tenant)


@router.delete("/tenants/{tenant_id}", response_model=TenantOperationResponse)
async def delete_operator_tenant(
    tenant_id: str,
    _: None = Depends(require_operator_auth),
) -> TenantOperationResponse:
    normalized_tenant = tenancy.operator_tenant_id(tenant_id)
    tenant_dir = tenancy.tenant_dir(normalized_tenant)
    removed_data = tenant_dir.exists()

    tenant_controller = tenancy.pop_tenant_controller(normalized_tenant)
    if tenant_controller is not None:
        await tenant_controller.shutdown()

    # Safe as written -- `operator_tenant_id` applies a strict regex and a URL
    # segment cannot contain a slash -- but a recursive delete should not rest
    # on an upstream validator staying strict, and this also catches a
    # symlinked tenant directory redirecting the delete somewhere else.
    tenancy.assert_within_tenant_root(tenant_dir)
    shutil.rmtree(tenant_dir, ignore_errors=True)
    return TenantOperationResponse(
        status="deleted",
        tenant_id=normalized_tenant,
        removed_cached_controller=tenant_controller is not None,
        removed_data=removed_data,
    )

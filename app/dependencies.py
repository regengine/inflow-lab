from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from .auth import DEFAULT_TENANT_ID, TenantContext
from .controller import SimulationController
from .tenancy import active_controller_for_context


def get_tenant_context(request: Request) -> TenantContext:
    return getattr(request.state, "tenant_context", TenantContext(tenant_id=DEFAULT_TENANT_ID))


def get_active_controller(
    context: TenantContext = Depends(get_tenant_context),
) -> SimulationController:
    return active_controller_for_context(context)


def require_operator_auth(context: TenantContext = Depends(get_tenant_context)) -> None:
    if not context.auth_enabled:
        raise HTTPException(status_code=403, detail="Tenant operations require Basic Auth")

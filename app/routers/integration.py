from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import HttpUrl

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT
from ..schemas.domain import DestinationMode
from ..schemas.integration import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    IntegrationConfigureRequest,
    IntegrationStatusResponse,
)
from ..schemas.simulation import EgressBlockedError, SimulationConfig


router = APIRouter(prefix="/api/integration", tags=["RegEngine Integration"])


def _delivery_host(config: SimulationConfig) -> str:
    """Host the stored config would actually deliver to right now (falls
    back to the documented default when no endpoint is configured)."""
    endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
    return (urlparse(endpoint).hostname or "").lower()


@router.get("/status", response_model=IntegrationStatusResponse)
async def integration_status(
    active_controller: SimulationController = Depends(get_active_controller),
) -> IntegrationStatusResponse:
    return active_controller.integration_status()


@router.post("/configure", response_model=IntegrationStatusResponse)
async def integration_configure(
    request: IntegrationConfigureRequest,
    active_controller: SimulationController = Depends(get_active_controller),
) -> IntegrationStatusResponse:
    return await active_controller.configure_integration(request)


@router.post("/test", response_model=ConnectionTestResponse)
async def integration_test(
    request: ConnectionTestRequest | None = None,
    active_controller: SimulationController = Depends(get_active_controller),
) -> ConnectionTestResponse:
    request = request or ConnectionTestRequest()
    config = active_controller.config
    updates: dict[str, HttpUrl | str | None] = {
        key: value
        for key, value in {
            "endpoint": request.endpoint,
            "api_key": request.api_key,
            "tenant_id": request.tenant_id,
        }.items()
        if value is not None
    }
    if request.endpoint is not None and (request.endpoint.host or "").lower() != _delivery_host(config):
        # Caller is pointing the probe at a different host than the stored/
        # default RegEngine endpoint. Never let the stored api_key/tenant_id
        # ride along to a host they were never issued for — the caller must
        # supply fresh credentials for THIS host (already captured above if
        # they did; the setdefault below is then a no-op). Otherwise the
        # probe runs uncredentialed and check_connection reports
        # not_configured instead of leaking them.
        updates.setdefault("api_key", None)
        updates.setdefault("tenant_id", None)
    delivery = config.delivery.model_copy(update=updates, deep=True)
    if config.delivery.mode == DestinationMode.MOCK and not (
        request.api_key or request.tenant_id or request.endpoint
    ):
        return ConnectionTestResponse(
            verdict="mock",
            detail=(
                "Delivery is in mock mode: events go to the built-in RegEngine "
                "stand-in, no credentials required. Enter live credentials to "
                "test a real connection."
            ),
            mode=config.delivery.mode,
        )

    probe_config = config.model_copy(update={"delivery": delivery}, deep=True)
    try:
        result = await active_controller.live_client.check_connection(probe_config)
    except EgressBlockedError as exc:
        # check_connection guards immediately before it dials, which is the
        # only point where the resolved address is the one actually used.
        # Surface the refusal as a clean 4xx rather than an unhandled 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConnectionTestResponse(
        verdict=result.verdict,
        detail=result.detail,
        status_code=result.status_code,
        endpoint_host=result.endpoint_host,
        mode=config.delivery.mode,
    )

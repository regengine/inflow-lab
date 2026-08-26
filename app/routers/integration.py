from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends

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


router = APIRouter(prefix="/api/integration", tags=["RegEngine Integration"])


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
    overrides = {
        key: value
        for key, value in {
            "endpoint": request.endpoint,
            "api_key": request.api_key,
            "tenant_id": request.tenant_id,
        }.items()
        if value is not None
    }
    # Never send the stored API key / tenant id to a host the caller just
    # named. Probing a different host requires passing its credentials
    # explicitly, otherwise `POST {"endpoint": "https://attacker.example"}`
    # would exfiltrate the saved RegEngine credentials in request headers.
    if request.endpoint is not None and _endpoint_host(str(request.endpoint)) != _endpoint_host(
        str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
    ):
        overrides.setdefault("api_key", None)
        overrides.setdefault("tenant_id", None)
    delivery = config.delivery.model_copy(update=overrides, deep=True)
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
    result = await active_controller.live_client.check_connection(probe_config)
    return ConnectionTestResponse(
        verdict=result.verdict,
        detail=result.detail,
        status_code=result.status_code,
        endpoint_host=result.endpoint_host,
        mode=config.delivery.mode,
    )


def _endpoint_host(endpoint: str) -> str:
    return (urlparse(endpoint).hostname or "").strip().lower().rstrip(".")

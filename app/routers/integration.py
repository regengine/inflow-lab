from __future__ import annotations

from fastapi import APIRouter, Depends

from ..controller import SimulationController
from ..dependencies import get_active_controller
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
    delivery = config.delivery.model_copy(
        update={
            key: value
            for key, value in {
                "endpoint": request.endpoint,
                "api_key": request.api_key,
                "tenant_id": request.tenant_id,
            }.items()
            if value is not None
        },
        deep=True,
    )
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

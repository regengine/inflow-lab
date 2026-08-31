from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException

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
    overrides: dict[str, object] = {
        key: value
        for key, value in {
            "endpoint": request.endpoint,
            "api_key": request.api_key,
            "tenant_id": request.tenant_id,
        }.items()
        if value is not None
    }
    # Only the supplied fields are overridden, so a request carrying just an
    # `endpoint` used to probe that host with the *stored* API key -- letting
    # any caller redirect the saved credential to a host of their choosing. If
    # the origin differs from the configured one, the caller has to bring its
    # own key rather than borrow the one on file.
    # Only when there is a stored credential to protect: with none saved, a
    # probe of any endpoint borrows nothing, and answering `not_configured` is
    # the correct and long-standing behaviour.
    if request.endpoint is not None and request.api_key is None and config.delivery.api_key:
        if _origin(request.endpoint) != _origin(config.delivery.endpoint):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Testing a different endpoint requires an api_key in the request; "
                    "the stored credential is only used for the configured endpoint."
                ),
            )
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


def _origin(endpoint: object) -> tuple[str, str, int | None] | None:
    """Scheme, host and port -- the identity that decides credential reuse.

    Compared on all three parts rather than the hostname alone, so neither an
    `http://` downgrade of the configured host nor a `https://user:pass@host/`
    userinfo prefix reads as the same origin.
    """
    if endpoint is None:
        return None
    parsed = urlparse(str(endpoint))
    return (parsed.scheme, (parsed.hostname or "").lower(), parsed.port)

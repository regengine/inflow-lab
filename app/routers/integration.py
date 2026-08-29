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


# Ports a scheme reaches when the URL does not spell one out, so
# "https://host" and "https://host:443" compare equal while "https://host"
# and "https://host:8443" do not.
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _origin(endpoint: str) -> str:
    """scheme://host:port for *endpoint* -- the identity a credential is issued for.

    Compared instead of the bare hostname (#209). Host-only comparison
    treated ``http://www.regengine.co`` as the same destination as
    ``https://www.regengine.co``, so a caller could hand the probe a
    downgraded URL and the stored API key -- issued for a TLS endpoint --
    would ride along over cleartext to anyone on the path. Scheme and port
    are part of what a credential was scoped to, so they are part of the
    comparison.

    Deliberately NOT normalized beyond the default-port case: a trailing
    dot ("www.regengine.co.") stays distinct from the bare host, and
    userinfo is already dropped by urlparse().hostname. Both of those are
    closed today and folding them together here would reopen them.
    """
    parsed = urlparse(endpoint)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        # An unparseable port is not a port we can prove matches, so give it
        # a value nothing else can equal rather than falling back to the
        # scheme default and calling it a match.
        return f"{scheme}://{host}:<invalid>"
    if port is None:
        port = _DEFAULT_SCHEME_PORTS.get(scheme)
    return f"{scheme}://{host}:{port}"


def _delivery_origin(config: SimulationConfig) -> str:
    """Origin the stored config would actually deliver to right now (falls
    back to the documented default when no endpoint is configured)."""
    endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
    return _origin(endpoint)


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
    stored_origin = _delivery_origin(config)
    probe_origin = _origin(str(request.endpoint)) if request.endpoint is not None else stored_origin
    withheld: list[str] = []
    if probe_origin != stored_origin:
        # Caller is pointing the probe at a different origin than the stored/
        # default RegEngine endpoint. Never let the stored api_key/tenant_id
        # ride along to an origin they were never issued for — the caller must
        # supply fresh credentials for THIS origin (already captured above if
        # they did; the setdefault below is then a no-op). Otherwise the
        # probe runs uncredentialed and reports not_configured instead of
        # leaking them.
        #
        # Which stored credentials that suppressed is recorded, because the
        # generic "no credentials configured" wording is factually false in
        # this case and the caller cannot act on it: they DID configure a
        # key, it just was not this origin's (#210).
        for field in ("api_key", "tenant_id"):
            if field not in updates and getattr(config.delivery, field):
                withheld.append(field)
            updates.setdefault(field, None)
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

    if withheld and not (delivery.api_key and delivery.tenant_id):
        # #210: check_connection's own not_configured detail -- "Both an API
        # key and a tenant id are required before testing the connection" --
        # names a condition that is not the one that failed here. Something
        # IS configured; it was withheld on purpose because this request
        # points somewhere else. Reporting the generic reason sent the
        # operator off to re-enter credentials that were already correct.
        # The verdict stays not_configured (nothing was probed, and the UI
        # and this suite both key off it); only the explanation changes.
        names = " and ".join(
            {"api_key": "API key", "tenant_id": "tenant id"}[field] for field in withheld
        )
        return ConnectionTestResponse(
            verdict="not_configured",
            detail=(
                f"Nothing was probed. The stored {names} belongs to {stored_origin}, "
                f"and this test targets {probe_origin}, so it was not reused — a "
                "credential is never sent to an origin it was not issued for. "
                "Supply an API key and tenant id for this origin to test it."
            ),
            endpoint_host=urlparse(str(delivery.endpoint)).netloc if delivery.endpoint else "",
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

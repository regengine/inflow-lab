from __future__ import annotations

from urllib.parse import urlparse

<<<<<<< HEAD
from fastapi import APIRouter, Depends, HTTPException
=======
from fastapi import APIRouter, Depends
>>>>>>> origin/main

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT
from ..schemas.domain import DestinationMode
from ..schemas.integration import (
    CREDENTIALS_WITHHELD_VERDICT,
    ConnectionTestRequest,
    ConnectionTestResponse,
    IntegrationConfigureRequest,
    IntegrationStatusResponse,
)

router = APIRouter(prefix="/api/integration", tags=["RegEngine Integration"])

# Default port per scheme, so an origin comparison never depends on whether the
# operator happened to spell the port out.
_DEFAULT_PORTS = {"http": 80, "https": 443}


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
<<<<<<< HEAD
    overrides: dict[str, object] = {
=======
    overrides = {
>>>>>>> origin/main
        key: value
        for key, value in {
            "endpoint": request.endpoint,
            "api_key": request.api_key,
            "tenant_id": request.tenant_id,
        }.items()
        if value is not None
    }
<<<<<<< HEAD
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
=======
    configured_endpoint = (
        str(config.delivery.endpoint)
        if config.delivery.endpoint
        else DEFAULT_LIVE_INGEST_ENDPOINT
    )
    # Never send the stored API key / tenant id to an endpoint the caller just
    # named. Probing a different endpoint requires passing its credentials
    # explicitly, otherwise `POST {"endpoint": "https://attacker.example"}`
    # would exfiltrate the saved RegEngine credentials in request headers.
    #
    # The comparison is on the full *origin*, not the bare host. Comparing
    # hostnames alone let three variants of the configured host inherit the
    # stored key: `http://` (the probe, and every header on it, then leaves in
    # cleartext), a different port (`https://host:8443` reaches whatever else
    # is listening on that host), and embedded userinfo. Each is a different
    # security context than the one the operator saved credentials for, so
    # each is treated exactly like a different host is.
    stored_credentials_withheld = False
    if request.endpoint is not None and not _same_origin(
        str(request.endpoint), configured_endpoint
    ):
        stored_credentials_withheld = bool(
            config.delivery.api_key or config.delivery.tenant_id
        )
        overrides.setdefault("api_key", None)
        overrides.setdefault("tenant_id", None)
>>>>>>> origin/main
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

    if stored_credentials_withheld and not (delivery.api_key and delivery.tenant_id):
        # The operator's credentials ARE configured; they were deliberately
        # withheld. Reporting `not_configured` here named a condition that had
        # not failed, and sent the operator off to re-enter credentials that
        # were already correct. Say what actually happened instead.
        return ConnectionTestResponse(
            verdict=CREDENTIALS_WITHHELD_VERDICT,
            detail=(
                "Nothing was sent. The stored API key and tenant id belong to "
                f"{_endpoint_origin_label(configured_endpoint)} and are never sent "
                f"anywhere else, so probing {_endpoint_origin_label(str(request.endpoint))} "
                "requires entering the API key and tenant id for that endpoint in this "
                "test. A different scheme, port, or host counts as a different endpoint."
            ),
            endpoint_host=_endpoint_host(str(request.endpoint)),
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


<<<<<<< HEAD
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
=======
def _endpoint_host(endpoint: str) -> str:
    return (urlparse(endpoint).hostname or "").strip().lower().rstrip(".")


def _endpoint_origin(endpoint: str) -> tuple[str, str, int] | None:
    """`(scheme, host, port)` for an endpoint, or None when it has no origin.

    The port is always explicit: a URL that omits it is normalised to its
    scheme's default (443/https, 80/http), so `https://h` and `https://h:443`
    are the same origin while `https://h:8443` is not. Host keeps the existing
    lowercase and trailing-dot normalisation, and the scheme is lowercased for
    the same reason.

    Returns None -- which never compares equal to any origin, including
    another None -- for anything whose origin cannot be established or is more
    than an origin: a missing scheme or host, a malformed port, or embedded
    userinfo. Userinfo is not part of an origin, but a caller who supplies it
    is asking for credentials of their own to travel with the request, which is
    not the saved endpoint the stored key was entrusted to.
    """
    parsed = urlparse(endpoint)
    if parsed.username is not None or parsed.password is not None:
        return None
    scheme = parsed.scheme.strip().lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not scheme or not host:
        return None
    try:
        port = parsed.port
    except ValueError:  # out-of-range or non-numeric port
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 0)
    return (scheme, host, port)


def _same_origin(candidate: str, configured: str) -> bool:
    """True only when both endpoints share one exact, well-formed origin."""
    candidate_origin = _endpoint_origin(candidate)
    return candidate_origin is not None and candidate_origin == _endpoint_origin(configured)


def _endpoint_origin_label(endpoint: str) -> str:
    """`scheme://host[:port]` for operator-facing text; never a path or secret."""
    origin = _endpoint_origin(endpoint)
    if origin is None:
        # No usable origin (userinfo, malformed port, no scheme/host). Name it
        # by what the caller did rather than printing a half-parsed URL back.
        return "the endpoint you supplied"
    scheme, host, port = origin
    if port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"
>>>>>>> origin/main

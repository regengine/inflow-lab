from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.integration import CREDENTIALS_WITHHELD_VERDICT
from app.schemas.simulation import SimulationConfig


client = TestClient(app)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def assert_json_omits(payload: object, *needles: str) -> None:
    dumped = json.dumps(payload, sort_keys=True)
    for needle in needles:
        assert needle not in dumped


class FakeProbeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class ProbeAsyncClient:
    status_code = 200
    # None -> /health raises (non-JSON); a dict -> served as the health body.
    health_payload: Any = None
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "ProbeAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        # extensions: the live client attaches {"sni_hostname": ...} when it
        # pins the validated address for the dial (#207). Accepted and
        # ignored here so this fake keeps working on either path.
        extensions: dict[str, Any] | None = None,
    ) -> FakeProbeResponse:
        if url.endswith("/health"):
            return FakeProbeResponse(200, ProbeAsyncClient.health_payload)
        ProbeAsyncClient.calls.append({"url": url, "headers": headers, "params": params})
        return FakeProbeResponse(ProbeAsyncClient.status_code)


def test_integration_status_defaults_to_mock_with_no_credentials() -> None:
    response = client.get("/api/integration/status")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "mock"
    assert body["endpoint"] == "https://www.regengine.co/api/v1/webhooks/ingest"
    assert body["endpoint_host"] == "www.regengine.co"
    assert body["api_key_configured"] is False
    assert body["tenant_configured"] is False
    assert body["mock_friction"] == []


def test_integration_configure_saves_credentials_without_echoing_secrets() -> None:
    response = client.post(
        "/api/integration/configure",
        json={
            "endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest",
            "api_key": "rge_live_super_secret_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["api_key_configured"] is True
    assert body["tenant_configured"] is True
    assert body["endpoint_host"] == "staging.regengine.example"
    assert_json_omits(body, "rge_live_super_secret_key")

    # Partial update: switching mode keeps the stored credentials.
    response = client.post("/api/integration/configure", json={"mode": "live"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "live"
    assert body["api_key_configured"] is True
    assert_json_omits(body, "rge_live_super_secret_key")


def test_integration_configure_rejects_live_mode_without_credentials() -> None:
    response = client.post("/api/integration/configure", json={"mode": "live"})
    assert response.status_code == 400
    assert "api_key" in response.json()["detail"]


def test_integration_test_reports_mock_mode_without_probing() -> None:
    response = client.post("/api/integration/test", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "mock"
    assert body["mode"] == "mock"


def test_integration_test_maps_probe_status_to_customer_verdicts(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ProbeAsyncClient)
    ProbeAsyncClient.health_payload = None
    expectations = {
        200: "connected",
        401: "unauthorized",
        402: "subscription_inactive",
        403: "forbidden",
        404: "tenant_mismatch",
        429: "rate_limited",
    }
    for status_code, verdict in expectations.items():
        ProbeAsyncClient.status_code = status_code
        ProbeAsyncClient.calls = []
        response = client.post(
            "/api/integration/test",
            json={
                "api_key": "rge_live_probe_key",
                "tenant_id": "22222222-2222-2222-2222-222222222222",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == verdict, (status_code, body)
        assert body["status_code"] == status_code
        assert_json_omits(body, "rge_live_probe_key")
        call = ProbeAsyncClient.calls[0]
        assert call["url"].endswith("/api/v1/webhooks/recent")
        assert call["headers"]["X-RegEngine-API-Key"] == "rge_live_probe_key"
        assert call["headers"]["X-Tenant-ID"] == "22222222-2222-2222-2222-222222222222"
        assert call["params"] == {
            "tenant_id": "22222222-2222-2222-2222-222222222222",
            "limit": 1,
        }


def test_connection_reports_contract_mismatch_when_versions_diverge(monkeypatch: Any) -> None:
    from app.contract import INFLOW_CONTRACT_VERSION

    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ProbeAsyncClient)
    ProbeAsyncClient.status_code = 200
    credentials = {
        "api_key": "rge_live_probe_key",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
    }

    # RegEngine advertises a different contract version -> mismatch verdict.
    ProbeAsyncClient.health_payload = {"status": "healthy", "inflow_contract_version": "999"}
    body = client.post("/api/integration/test", json=credentials).json()
    assert body["verdict"] == "contract_mismatch"
    assert "999" in body["detail"]
    assert INFLOW_CONTRACT_VERSION in body["detail"]

    # Same version -> connected.
    ProbeAsyncClient.health_payload = {
        "status": "healthy",
        "inflow_contract_version": INFLOW_CONTRACT_VERSION,
    }
    body = client.post("/api/integration/test", json=credentials).json()
    assert body["verdict"] == "connected"

    # Older RegEngine (no version field) or non-JSON /health -> no mismatch.
    for payload in ({"status": "healthy"}, None):
        ProbeAsyncClient.health_payload = payload
        body = client.post("/api/integration/test", json=credentials).json()
        assert body["verdict"] == "connected", payload


def test_healthz_and_integration_status_advertise_contract_version() -> None:
    from app.contract import INFLOW_CONTRACT_VERSION

    healthz = client.get("/api/healthz").json()
    assert healthz["contract_version"] == INFLOW_CONTRACT_VERSION
    health = client.get("/api/health").json()
    assert health["contract_version"] == INFLOW_CONTRACT_VERSION
    status = client.get("/api/integration/status").json()
    assert status["contract_version"] == INFLOW_CONTRACT_VERSION


def test_integration_test_requires_credentials_for_live_probe() -> None:
    """Nothing is configured here, so `not_configured` is the truthful verdict.

    Contrast `test_integration_test_names_withheld_credentials_truthfully`
    below: when credentials *are* configured and were deliberately withheld
    because the probed endpoint is a different origin, the verdict must not be
    this one -- re-entering credentials is not the fix for that case.
    """
    response = client.post(
        "/api/integration/test",
        json={"endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest"},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "not_configured"


def test_integration_test_names_withheld_credentials_truthfully() -> None:
    configure = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_settings_secret",
            "tenant_id": "44444444-4444-4444-4444-444444444444",
        },
    )
    assert configure.status_code == 200

    # Same host, downgraded scheme: a different origin, so the stored key stays
    # put and the operator is told why instead of being sent to re-enter it.
    response = client.post(
        "/api/integration/test",
        json={"endpoint": "http://www.regengine.co/api/v1/webhooks/ingest"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == CREDENTIALS_WITHHELD_VERDICT
    assert body["verdict"] != "not_configured"
    assert_json_omits(body, "rge_live_settings_secret")


def test_mock_friction_surfaces_the_402_subscription_gate_then_recovers() -> None:
    response = client.post(
        "/api/integration/configure",
        json={"mock_friction": ["subscription_inactive"]},
    )
    assert response.status_code == 200
    assert response.json()["mock_friction"] == ["subscription_inactive"]

    step = client.post("/api/simulate/step").json()
    assert step["delivery_status"] == "failed"
    assert step["posted"] == 0
    assert "HTTP 402" in step["error"]

    events = client.get("/api/events?limit=10").json()["events"]
    assert events, "friction step should still persist records"
    assert events[0]["delivery_status"] == "failed"
    assert events[0]["delivery_metadata"]["status_code"] == 402
    original_key = events[0]["delivery_metadata"]["idempotency_key"]
    assert original_key

    # Operator clears the friction (billing reactivated) and retries: the
    # stored idempotency key is reused and delivery succeeds.
    client.post("/api/integration/configure", json={"mock_friction": []})
    retry = client.post("/api/delivery/retry", json={}).json()
    assert retry["status"] == "posted"
    assert retry["failed"] == 0
    events = client.get("/api/events?limit=10").json()["events"]
    assert events[0]["delivery_status"] == "posted"
    assert events[0]["delivery_metadata"]["idempotency_key"] == original_key


def test_mock_rate_limit_friction_maps_to_429() -> None:
    client.post("/api/integration/configure", json={"mock_friction": ["rate_limit"]})
    step = client.post("/api/simulate/step").json()
    assert step["delivery_status"] == "failed"
    assert "HTTP 429" in step["error"]
    events = client.get("/api/events?limit=5").json()["events"]
    assert events[0]["delivery_metadata"]["status_code"] == 429


def test_mock_idempotency_replay_returns_cached_response() -> None:
    from app.mock_service import MockRegEngineService
    from app.demo_fixtures import get_demo_fixture
    from app.schemas.domain import DemoFixtureId
    from app.schemas.ingestion import IngestPayload

    fixture = get_demo_fixture(DemoFixtureId.LEAFY_GREENS_TRACE)
    payload = IngestPayload(source="replay-check", events=[fe.event for fe in fixture.events])
    service = MockRegEngineService()
    first = service.ingest(payload, idempotency_key="replay-key-1")
    second = service.ingest(payload, idempotency_key="replay-key-1")
    assert first is second, "same idempotency key must replay the cached response"
    fresh = service.ingest(payload, idempotency_key="replay-key-2")
    assert fresh is not first
    first_ids = [event.event_id for event in first.events]
    fresh_ids = [event.event_id for event in fresh.events]
    assert first_ids != fresh_ids


def test_not_configured_detail_names_the_origin_mismatch_not_missing_credentials() -> None:
    """The verdict was right and the reason was false (#210).

    With credentials stored for one origin, probing a different one
    correctly withholds them -- but the detail that came back was
    check_connection's generic "Both an API key and a tenant id are
    required before testing the connection." Both ARE configured. An
    operator reading that goes and re-enters credentials that were already
    correct, and never learns that what actually happened is that this
    request pointed somewhere the stored key was not issued for.
    """
    configured = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_configured_key",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert configured.status_code == 200

    body = client.post(
        "/api/integration/test",
        json={"endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest"},
    ).json()

    assert body["verdict"] == "not_configured"
    detail = body["detail"]
    assert "Both an API key and a tenant id are required" not in detail, (
        "reported missing credentials that are in fact configured"
    )
    assert "https://www.regengine.co:443" in detail, "should name where the stored key belongs"
    assert "https://staging.regengine.example:443" in detail, "should name what was targeted"
    assert "rge_live_configured_key" not in detail


def test_not_configured_detail_is_unchanged_when_nothing_is_actually_configured() -> None:
    # The generic wording is correct in the case it was written for, and
    # must survive. setup_function has reset to a default config, so
    # nothing is stored and nothing is supplied.
    body = client.post(
        "/api/integration/test",
        json={"endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest"},
    ).json()

    assert body["verdict"] == "not_configured"
    assert "Both an API key and a tenant id are required" in body["detail"]

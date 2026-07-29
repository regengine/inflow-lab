from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.simulation import SimulationConfig


client = TestClient(app)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def assert_json_omits(payload: object, *needles: str) -> None:
    dumped = json.dumps(payload, sort_keys=True)
    for needle in needles:
        assert needle not in dumped


class FakeProbeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class ProbeAsyncClient:
    status_code = 200
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "ProbeAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, Any]) -> FakeProbeResponse:
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


def test_integration_test_requires_credentials_for_live_probe() -> None:
    response = client.post(
        "/api/integration/test",
        json={"endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest"},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "not_configured"


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

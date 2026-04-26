from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.models import CTEType, IngestPayload, RegEngineEvent, SimulationConfig
from app.regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT, LiveRegEngineClient


class FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"accepted": 1}


class RecordingAsyncClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "RecordingAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, endpoint: str, *, headers: dict[str, str], json: dict[str, Any]) -> FakeResponse:
        self.calls.append(
            {
                "endpoint": endpoint,
                "headers": headers,
                "json": json,
                "timeout": self.timeout,
            }
        )
        return FakeResponse()


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
                kdes={
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                },
            )
        ],
    )


def make_live_config(endpoint: str | None = None) -> SimulationConfig:
    return SimulationConfig(
        delivery={
            "mode": "live",
            "endpoint": endpoint,
            "api_key": "test-api-key",
            "tenant_id": "test-tenant-id",
        }
    )


def run_ingest(monkeypatch: Any, config: SimulationConfig) -> dict[str, Any]:
    RecordingAsyncClient.calls = []
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", RecordingAsyncClient)

    result = asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert result.response == {"accepted": 1}
    assert result.metadata["delivery_mode"] == "live"
    assert result.metadata["endpoint_host"]
    assert result.metadata["endpoint_path"]
    assert result.metadata["idempotency_key"]
    assert result.metadata["status_code"] == 200
    assert len(RecordingAsyncClient.calls) == 1
    return RecordingAsyncClient.calls[0]


def test_live_client_uses_documented_default_endpoint(monkeypatch: Any) -> None:
    call = run_ingest(monkeypatch, make_live_config())

    assert call["endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT
    assert call["endpoint"] == "https://www.regengine.co/api/v1/webhooks/ingest"


def test_live_client_uses_configured_endpoint_override(monkeypatch: Any) -> None:
    override = "https://partner.example.test/regengine/ingest"

    call = run_ingest(monkeypatch, make_live_config(endpoint=override))

    assert call["endpoint"] == override


def test_live_client_sends_required_headers_and_contract_payload(monkeypatch: Any) -> None:
    call = run_ingest(monkeypatch, make_live_config())

    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["X-RegEngine-API-Key"] == "test-api-key"
    assert call["headers"]["X-Tenant-ID"] == "test-tenant-id"
    assert call["headers"]["Idempotency-Key"]

    payload = call["json"]
    assert set(payload) == {"source", "events"}
    assert payload["source"] == "erp"
    assert len(payload["events"]) == 1

    event = payload["events"][0]
    assert set(event) == {
        "cte_type",
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "location_name",
        "timestamp",
        "kdes",
    }
    assert event == {
        "cte_type": "receiving",
        "traceability_lot_code": "00012345678901-LOT-2026-001",
        "product_description": "Romaine Lettuce",
        "quantity": 500.0,
        "unit_of_measure": "cases",
        "location_name": "Distribution Center #4",
        "timestamp": "2026-02-05T08:30:00Z",
        "kdes": {
            "receive_date": "2026-02-05",
            "receiving_location": "Distribution Center #4",
            "ship_from_location": "Valley Fresh Farms",
        },
    }

import asyncio
from datetime import UTC, datetime

from app.models import CTEType, DeliveryConfig, DestinationMode, IngestPayload, RegEngineEvent, SimulationConfig
from app.regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT, LiveRegEngineClient


def _payload() -> IngestPayload:
    return IngestPayload(
        source="test-suite",
        events=[
            RegEngineEvent(
                cte_type=CTEType.RECEIVING,
                traceability_lot_code="TLC-TEST-000001",
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


def test_live_client_uses_documented_default_endpoint_and_headers(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            captured["raise_for_status_called"] = True

        def json(self):
            return {"accepted": 1, "events": []}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, endpoint, *, headers, json):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", FakeAsyncClient)
    config = SimulationConfig(
        delivery=DeliveryConfig(
            mode=DestinationMode.LIVE,
            api_key="regengine-api-key",
            tenant_id="tenant-123",
        )
    )

    result = asyncio.run(LiveRegEngineClient().ingest(_payload(), config))

    assert result == {"accepted": 1, "events": []}
    assert captured["endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT
    assert captured["timeout"] == 20.0
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "X-RegEngine-API-Key": "regengine-api-key",
        "X-Tenant-ID": "tenant-123",
        "Idempotency-Key": captured["headers"]["Idempotency-Key"],
    }
    assert captured["headers"]["Idempotency-Key"]
    assert captured["json"]["source"] == "test-suite"
    assert captured["json"]["events"][0]["traceability_lot_code"] == "TLC-TEST-000001"
    assert captured["json"]["events"][0]["timestamp"] == "2026-02-05T08:30:00Z"
    assert captured["raise_for_status_called"] is True

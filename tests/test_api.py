import pytest
from fastapi.testclient import TestClient

from app.main import app, controller


client = TestClient(app)

SECRET = "regengine-live-secret"
PUBLIC_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"


def setup_function() -> None:
    # reset shared app state between tests
    import asyncio

    asyncio.run(controller.reset())


def test_single_step_generates_mock_events():
    response = client.post("/api/simulate/step")
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated"] == 3
    assert len(payload["lot_codes"]) == 3
    assert payload["accepted"] == 3
    assert payload["rejected"] == 0

    events_response = client.get("/api/events?limit=10")
    assert events_response.status_code == 200
    events = events_response.json()["events"]
    assert len(events) == 3
    first_event = events[0]["event"]
    assert "cte_type" in first_event
    assert "traceability_lot_code" in first_event
    assert "kdes" in first_event


def test_mock_ingest_endpoint_returns_hashes():
    payload = {
        "source": "test-suite",
        "events": [
            {
                "cte_type": "receiving",
                "traceability_lot_code": "TLC-TEST-000001",
                "product_description": "Romaine Lettuce",
                "quantity": 500,
                "unit_of_measure": "cases",
                "location_name": "Distribution Center #4",
                "timestamp": "2026-02-05T08:30:00Z",
                "kdes": {
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                },
            }
        ],
    }
    response = client.post("/api/mock/regengine/ingest", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["events"][0]["status"] == "accepted"
    assert body["events"][0]["sha256_hash"]
    assert body["events"][0]["chain_hash"]


def test_fda_export_shape_contains_expected_columns():
    client.post("/api/simulate/step")
    response = client.get("/api/mock/regengine/export/fda-request")
    assert response.status_code == 200
    csv_text = response.text
    assert "Traceability Lot Code" in csv_text
    assert "Location Identifier (GLN)" in csv_text
    assert "Reference Document Number" in csv_text


def _config(delivery: dict | None = None) -> dict:
    return {
        "source": "test-suite",
        "interval_seconds": 999,
        "batch_size": 2,
        "seed": 204,
        "persist_path": "data/events.jsonl",
        "delivery": {
            "mode": "mock",
            "endpoint": PUBLIC_ENDPOINT,
            "api_key": None,
            "tenant_id": None,
            "live_confirmed": False,
            **(delivery or {}),
        },
    }


def test_live_start_requires_explicit_confirmation():
    response = client.post(
        "/api/simulate/start",
        json={
            "config": _config(
                {
                    "mode": "live",
                    "api_key": SECRET,
                    "tenant_id": "tenant-123",
                    "live_confirmed": False,
                }
            )
        },
    )

    assert response.status_code == 400
    assert "delivery.live_confirmed" in response.json()["detail"]


def test_live_step_requires_explicit_confirmation():
    response = client.post(
        "/api/simulate/step",
        json={
            "config": _config(
                {
                    "mode": "live",
                    "api_key": SECRET,
                    "tenant_id": "tenant-123",
                    "live_confirmed": False,
                }
            )
        },
    )

    assert response.status_code == 400
    assert "delivery.live_confirmed" in response.json()["detail"]


@pytest.mark.parametrize(
    ("missing_field", "delivery_override"),
    [
        ("delivery.endpoint", {"endpoint": None}),
        ("delivery.api_key", {"api_key": None}),
        ("delivery.tenant_id", {"tenant_id": None}),
    ],
)
def test_live_start_rejects_missing_required_live_fields(missing_field, delivery_override):
    delivery = {
        "mode": "live",
        "api_key": SECRET,
        "tenant_id": "tenant-123",
        "live_confirmed": True,
        **delivery_override,
    }

    response = client.post("/api/simulate/start", json={"config": _config(delivery)})

    assert response.status_code == 400
    assert missing_field in response.json()["detail"]


def test_health_status_and_start_mask_api_key():
    config = _config({"api_key": SECRET})

    reset_response = client.post("/api/simulate/reset", json=config)
    assert reset_response.status_code == 200
    assert SECRET not in reset_response.text

    status_response = client.get("/api/simulate/status")
    assert status_response.status_code == 200
    status = status_response.json()
    assert status["config"]["delivery"]["api_key"] == "***MASKED***"
    assert SECRET not in status_response.text

    health_response = client.get("/api/health")
    assert health_response.status_code == 200
    assert health_response.json()["status"]["config"]["delivery"]["api_key"] == "***MASKED***"
    assert SECRET not in health_response.text

    start_response = client.post("/api/simulate/start", json={"config": config})
    assert start_response.status_code == 200
    assert start_response.json()["config"]["delivery"]["api_key"] == "***MASKED***"
    assert SECRET not in start_response.text
    client.post("/api/simulate/stop")


def test_jsonl_persistence_never_contains_api_key():
    config = _config({"api_key": SECRET})
    client.post("/api/simulate/reset", json=config)

    response = client.post("/api/simulate/step")

    assert response.status_code == 200
    persist_path = controller.store.persist_path
    assert persist_path.exists()
    persisted = persist_path.read_text(encoding="utf-8")
    assert SECRET not in persisted
    assert "api_key" not in persisted


def test_rejected_live_response_is_counted_as_rejected(monkeypatch):
    async def fake_ingest(payload, config):
        return {
            "accepted": 0,
            "rejected": len(payload.events),
            "total": len(payload.events),
            "events": [
                {
                    "traceability_lot_code": event.traceability_lot_code,
                    "cte_type": event.cte_type.value,
                    "status": "rejected",
                    "errors": ["Missing required KDE 'reference_document'"],
                }
                for event in payload.events
            ],
        }

    monkeypatch.setattr(controller.live_client, "ingest", fake_ingest)

    response = client.post(
        "/api/simulate/step",
        json={
            "config": _config(
                {
                    "mode": "live",
                    "api_key": SECRET,
                    "tenant_id": "tenant-123",
                    "live_confirmed": True,
                }
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["posted"] == 2
    assert body["accepted"] == 0
    assert body["rejected"] == 2
    assert body["failed"] == 0

    events = client.get("/api/events?limit=10").json()["events"]
    assert {event["delivery_status"] for event in events} == {"rejected"}

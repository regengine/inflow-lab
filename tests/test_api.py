"""Core simulation API: step, status, SSE, the mock ingest endpoint and
run-loop lifecycle.

The rest of this file was split by theme into ``test_api_*.py`` modules
(see #132); the tests themselves were moved verbatim.
"""

from datetime import UTC, datetime, timedelta

import asyncio
import base64
import json

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.simulation import SimulationConfig
from app.scenarios import ScenarioId, get_scenario


client = TestClient(app)

SECRET = "regengine-live-secret"
PUBLIC_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def assert_json_omits(payload: object, *needles: str) -> None:
    dumped = json.dumps(payload, sort_keys=True)
    for needle in needles:
        assert needle not in dumped


def setup_function() -> None:
    # reset shared app state between tests
    import asyncio

    asyncio.run(controller.reset(SimulationConfig()))


def test_single_step_generates_mock_events():
    response = client.post("/api/simulate/step")
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated"] == 3
    assert len(payload["lot_codes"]) == 3
    assert payload["posted"] == 3
    assert payload["failed"] == 0

    events_response = client.get("/api/events?limit=10")
    assert events_response.status_code == 200
    events = events_response.json()["events"]
    assert len(events) == 3
    first_event = events[0]["event"]
    assert "cte_type" in first_event
    assert "traceability_lot_code" in first_event
    assert "kdes" in first_event


def test_sse_stream_emits_initial_snapshot():
    response = client.get("/api/simulate/stream?limit=5&once=true")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.splitlines() if line]

    assert lines[0] == "event: snapshot"
    data_line = next(line for line in lines if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["revision"] == controller.revision
    assert payload["status"]["running"] is False
    assert payload["events"] == []


def test_status_surfaces_redact_live_delivery_credentials():
    api_key = "regengine-live-api-key-secret"
    tenant_id = "regengine-live-tenant-secret"
    reset_response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "delivery": {
                "mode": "live",
                "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
                "api_key": api_key,
                "tenant_id": tenant_id,
            },
        },
    )
    assert reset_response.status_code == 200

    status = client.get("/api/simulate/status").json()
    health = client.get("/api/health").json()
    stream_response = client.get("/api/simulate/stream?limit=5&once=true")
    stream_data = next(
        line.removeprefix("data: ")
        for line in stream_response.text.splitlines()
        if line.startswith("data: ")
    )
    snapshot = json.loads(stream_data)

    for payload in (status, health, snapshot):
        assert_json_omits(payload, api_key, tenant_id)

    delivery = status["config"]["delivery"]
    assert delivery["mode"] == "live"
    assert delivery["endpoint"] == "https://www.regengine.co/api/v1/webhooks/ingest"
    assert delivery["api_key"] is None
    assert delivery["tenant_id"] is None
    assert health["status"]["config"]["delivery"] == delivery
    assert snapshot["status"]["config"]["delivery"] == delivery


def test_scenario_catalog_endpoint_lists_supported_presets():
    response = client.get("/api/scenarios")

    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert [scenario["id"] for scenario in scenarios] == [
        "leafy_greens_supplier",
        "fresh_cut_processor",
        "retailer_readiness_demo",
        "seafood_first_receiver",
        "dairy_continuous_flow",
        "copacker_nut_butter",
        "broadline_distributor",
        "foodservice_restaurant_group",
        "shell_egg_producer",
    ]
    assert all(scenario["label"] for scenario in scenarios)
    assert {scenario["industry_type"] for scenario in scenarios} >= {"produce", "seafood", "dairy"}
    assert {scenario["operation_type"] for scenario in scenarios} >= {"supplier", "processor", "retailer", "first_receiver"}


def test_status_includes_backend_audit_summary():
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "leafy_greens_supplier",
            "batch_size": 1,
            "seed": 204,
        },
    )
    client.post("/api/simulate/step")

    status = client.get("/api/simulate/status").json()
    audit = status["stats"]["audit"]

    assert audit["industry_type"] == "produce"
    assert audit["reference_format"] == "GS1"
    assert isinstance(audit["score"], int)
    assert audit["total"] >= 1
    assert isinstance(audit["checks"], list)


def test_status_audit_tracks_seafood_readiness_shape():
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "seafood_first_receiver",
            "batch_size": 1,
            "seed": 204,
        },
    )
    client.post("/api/simulate/step")

    status = client.get("/api/simulate/status").json()
    audit = status["stats"]["audit"]

    assert audit["industry_type"] == "seafood"
    assert audit["requires_cooling"] is False
    assert any(check["label"] == "Vessel-linked receiving" for check in audit["checks"])


def test_controller_revision_notifies_after_step():
    async def wait_for_step_update() -> dict:
        starting_revision = controller.revision
        waiter = asyncio.create_task(controller.wait_for_revision(starting_revision, timeout=1.0))
        await controller.step(batch_size=1)
        observed_revision = await waiter
        return controller.snapshot(event_limit=5) | {"observed_revision": observed_revision}

    snapshot = asyncio.run(wait_for_step_update())

    assert snapshot["observed_revision"] == snapshot["revision"]
    assert snapshot["status"]["stats"]["total_records"] == 1
    assert len(snapshot["events"]) == 1


def test_stop_interrupts_long_interval_sleep(tmp_path):
    async def start_and_stop() -> bool:
        await controller.start(
            SimulationConfig(
                interval_seconds=60,
                batch_size=1,
                seed=204,
                persist_path=str(tmp_path / "long-interval-events.jsonl"),
            )
        )
        await asyncio.wait_for(controller.stop(), timeout=1.0)
        return controller.running

    assert asyncio.run(start_and_stop()) is False


# Built relative to "now": the mock enforces RegEngine's 90-day replay window
# by default, so a pinned calendar timestamp is rejected once it goes stale.
RECENT_MOMENT = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)


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
                "timestamp": RECENT_MOMENT.isoformat().replace("+00:00", "Z"),
                "kdes": {
                    "receive_date": RECENT_MOMENT.date().isoformat(),
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                    "immediate_previous_source": "Valley Fresh Farms",
                    "reference_document": "Bill of Lading BOL-TEST-0001",
                    "tlc_source_reference": "SRC-TEST-0001",
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


def test_mock_ingest_rejects_events_missing_required_kdes_like_regengine():
    payload = {
        "source": "test-suite",
        "events": [
            {
                "cte_type": "receiving",
                "traceability_lot_code": "TLC-TEST-REJECT-01",
                "product_description": "Romaine Lettuce",
                "quantity": 500,
                "unit_of_measure": "cases",
                "location_name": "Distribution Center #4",
                "timestamp": "2026-02-05T08:30:00Z",
                # reference_document_type/number deliberately do NOT satisfy
                # the combined reference_document key: the live validator uses
                # strict string lookup, and the mock must match it.
                "kdes": {
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "reference_document_type": "Bill of Lading",
                    "reference_document_number": "BOL-TEST-0002",
                },
            }
        ],
    }
    response = client.post("/api/mock/regengine/ingest", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 0
    assert body["rejected"] == 1
    event_result = body["events"][0]
    assert event_result["status"] == "rejected"
    assert event_result["event_id"] is None
    assert event_result["sha256_hash"] is None
    joined_errors = " ".join(event_result["errors"])
    assert "immediate_previous_source" in joined_errors
    assert "reference_document" in joined_errors
    assert "tlc_source_reference" in joined_errors


def test_reset_applies_scenario_config_and_keeps_mock_delivery_default(tmp_path):
    custom_path = tmp_path / "retailer-events.jsonl"
    response = client.post(
        "/api/simulate/reset",
        json={
            "scenario": "retailer_readiness_demo",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == "retailer_readiness_demo"
    assert status["config"]["delivery"]["mode"] == "mock"
    assert status["stats"]["engine"]["scenario"] == "retailer_readiness_demo"

    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200
    events = client.get("/api/events?limit=1").json()["events"]
    expected_products = {product.name for product in get_scenario(ScenarioId.RETAILER_READINESS_DEMO).products}

    assert events[0]["event"]["product_description"] in expected_products


def test_start_applies_scenario_change_even_with_existing_records(tmp_path):
    custom_path = tmp_path / "scenario-switch-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "leafy_greens_supplier",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    client.post("/api/simulate/step")

    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "scenario": "fresh_cut_processor",
                "interval_seconds": 10,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
            }
        },
    )
    try:
        assert response.status_code == 200
        status = response.json()
        assert status["config"]["scenario"] == "fresh_cut_processor"
        assert status["stats"]["engine"]["scenario"] == "fresh_cut_processor"
    finally:
        client.post("/api/simulate/stop")

"""HTTP API surface guarantees: strict bodies, bounded responses, typed docs.

Covers the contract-hygiene fixes that are easy to regress silently —
a dropped unknown field, an empty filter that means "everything", an
unbounded lineage response, an endpoint that documents nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app, controller
from app.regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT
from app.routers.events import LINEAGE_DEFAULT_LIMIT, LINEAGE_MAX_LIMIT
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.simulation import SimulationConfig
from app.scenarios import ScenarioId
from app.store import MASKED_SECRET, EventStore


client = TestClient(app)

BASE_TIME = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)


def setup_function() -> None:
    import asyncio

    asyncio.run(controller.reset(SimulationConfig()))


def make_record(
    lot_code: str,
    minutes: int = 0,
    parent_lot_codes: list[str] | None = None,
    delivery_status: str = "generated",
    kdes: dict | None = None,
) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=BASE_TIME + timedelta(minutes=minutes),
            kdes=kdes or {},
        ),
        parent_lot_codes=parent_lot_codes or [],
        destination_mode=DestinationMode.NONE,
        delivery_status=delivery_status,
    )


# --- #143: unknown / misplaced request fields are rejected ----------------


def test_reset_accepts_the_same_wrapped_config_shape_start_uses():
    response = client.post(
        "/api/simulate/reset",
        json={"config": {"scenario": ScenarioId.DAIRY_CONTINUOUS_FLOW.value, "batch_size": 1}},
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == ScenarioId.DAIRY_CONTINUOUS_FLOW.value
    assert status["config"]["batch_size"] == 1


def test_reset_still_accepts_the_historical_flat_config_shape():
    response = client.post(
        "/api/simulate/reset",
        json={"scenario": ScenarioId.DAIRY_CONTINUOUS_FLOW.value, "batch_size": 2},
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == ScenarioId.DAIRY_CONTINUOUS_FLOW.value
    assert status["config"]["batch_size"] == 2


def test_reset_with_no_body_resets_to_defaults():
    assert client.post("/api/simulate/reset").status_code == 200
    assert client.post("/api/simulate/reset", json={}).status_code == 200


def test_unknown_reset_field_is_rejected_instead_of_dropped():
    response = client.post(
        "/api/simulate/reset",
        json={"scenario": ScenarioId.DAIRY_CONTINUOUS_FLOW.value, "scenarios": "typo"},
    )
    assert response.status_code == 422
    assert "scenarios" in json.dumps(response.json())


def test_start_rejects_a_flat_config_body():
    response = client.post("/api/simulate/start", json={"batch_size": 1, "seed": 204})
    assert response.status_code == 422


def test_unknown_delivery_field_is_rejected():
    response = client.post(
        "/api/simulate/reset",
        json={"batch_size": 1, "delivery": {"mode": "mock", "endpoints": "typo"}},
    )
    assert response.status_code == 422


def test_unknown_integration_configure_field_is_rejected():
    response = client.post("/api/integration/configure", json={"mode": "mock", "api_keys": "typo"})
    assert response.status_code == 422


def test_unknown_delivery_retry_field_is_rejected():
    response = client.post("/api/delivery/retry", json={"record_id": "not-a-list"})
    assert response.status_code == 422


def test_mock_ingest_stays_lenient_about_additive_fields():
    """The mock stands in for RegEngine's receiver, which must not 422 on extras."""
    response = client.post(
        "/api/mock/regengine/ingest",
        json={
            "source": "test-suite",
            "events": [
                {
                    "cte_type": "harvesting",
                    "traceability_lot_code": "TLC-LENIENT-1",
                    "product_description": "Romaine Lettuce",
                    "quantity": 10,
                    "unit_of_measure": "cases",
                    "location_name": "Valley Fresh Farms",
                    "timestamp": BASE_TIME.isoformat(),
                    "kdes": {"harvest_date": "2026-03-04"},
                }
            ],
            "future_producer_field": "ignored, not rejected",
        },
    )
    assert response.status_code == 200


# --- #144: empty record_ids means "nothing", not "everything" -------------


def test_empty_record_ids_retries_nothing(tmp_path):
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.add_many([make_record("TLC-FAILED-1", delivery_status="failed")])

    assert store.failed_delivery_records([]) == []
    assert [record.event.traceability_lot_code for record in store.failed_delivery_records()] == [
        "TLC-FAILED-1"
    ]
    assert [record.event.traceability_lot_code for record in store.failed_delivery_records(None)] == [
        "TLC-FAILED-1"
    ]
    assert store.failed_delivery_records(["TLC-NOT-STORED"]) == []


def test_retry_endpoint_with_empty_record_ids_is_a_no_op():
    controller.store.add_many([make_record("TLC-RETRY-EMPTY", delivery_status="failed")])

    empty = client.post(
        "/api/delivery/retry", json={"record_ids": [], "delivery": {"mode": "mock"}}
    ).json()
    assert empty["status"] == "empty"
    assert empty["requested"] == 0
    assert empty["attempted"] == 0

    everything = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}}).json()
    assert everything["attempted"] >= 1


# --- #145: lineage responses are bounded and say so -----------------------


def test_lineage_reports_totals_and_is_untruncated_by_default():
    client.post("/api/simulate/reset", json={"config": {"batch_size": 3, "seed": 204}})
    client.post("/api/simulate/step")
    lot_code = client.get("/api/events").json()["events"][0]["event"]["traceability_lot_code"]

    body = client.get(f"/api/lineage/{lot_code}").json()
    assert body["truncated"] is False
    assert body["limit"] == LINEAGE_DEFAULT_LIMIT
    assert body["returned_records"] == len(body["records"]) == body["total_records"]


def test_lineage_truncates_at_the_limit_and_flags_it():
    client.post("/api/simulate/reset", json={"config": {"batch_size": 3, "seed": 204}})
    client.post("/api/simulate/step")
    client.post("/api/simulate/step")
    lot_code = client.get("/api/events").json()["events"][0]["event"]["traceability_lot_code"]

    full = client.get(f"/api/lineage/{lot_code}").json()
    assert full["total_records"] >= 2

    capped = client.get(f"/api/lineage/{lot_code}", params={"limit": 1}).json()
    assert capped["truncated"] is True
    assert capped["limit"] == 1
    assert capped["returned_records"] == len(capped["records"]) == 1
    assert capped["total_records"] == full["total_records"]
    # nodes/edges describe only what was returned, never unseen records.
    assert {node["lot_code"] for node in capped["nodes"]} == {
        record["event"]["traceability_lot_code"] for record in capped["records"]
    }


def test_lineage_limit_is_bounded_on_both_ends():
    assert client.get("/api/lineage/TLC-ANY", params={"limit": 0}).status_code == 422
    assert (
        client.get("/api/lineage/TLC-ANY", params={"limit": LINEAGE_MAX_LIMIT + 1}).status_code == 422
    )


# --- #146: health and healthz document their fields -----------------------


def test_health_endpoints_publish_concrete_openapi_schemas():
    schema = app.openapi()
    for path, model_name in (("/api/health", "HealthResponse"), ("/api/healthz", "HealthzResponse")):
        ref = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert ref == {"$ref": f"#/components/schemas/{model_name}"}
        properties = schema["components"]["schemas"][model_name]["properties"]
        assert {"ok", "utc_time", "build", "contract_version", "store"} <= set(properties)

    assert {"tenant", "auth", "status"} <= set(
        schema["components"]["schemas"]["HealthResponse"]["properties"]
    )


def test_health_endpoints_still_return_their_full_bodies():
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert body["store"]["ok"] is True
    assert body["build"]["version"]
    assert body["status"]["config"]["scenario"]

    healthz = client.get("/api/healthz").json()
    assert healthz["ok"] is True
    assert healthz["store"]["ok"] is True


# --- #90: every write path scrubs secrets --------------------------------


def test_all_three_write_paths_mask_secret_named_fields(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    secret = "regengine-live-api-key-secret"
    record = make_record("TLC-SECRET-1", kdes={"api_key": secret})

    stored = store.add_many([record])[0]
    assert secret not in persist_path.read_text(encoding="utf-8")

    # update_many (the delivery-retry rewrite) must not un-mask it.
    stored.delivery_status = "failed"
    stored.event.kdes["api_key"] = secret
    store.update_many([stored])
    on_disk = persist_path.read_text(encoding="utf-8")
    assert secret not in on_disk
    assert MASKED_SECRET in on_disk

    # replace_all (the scenario-load rewrite) must not either.
    reloaded = make_record("TLC-SECRET-2", kdes={"api_key": secret})
    reloaded.sequence_no = 1
    store.replace_all([reloaded])
    on_disk = persist_path.read_text(encoding="utf-8")
    assert secret not in on_disk
    assert MASKED_SECRET in on_disk


def test_rewrites_stay_durable_and_leave_no_temp_file(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-DURABLE-1"), make_record("TLC-DURABLE-2", minutes=5)])

    store.replace_all(store.recent(limit=10))
    assert not list(tmp_path.glob("*.tmp"))

    reopened = EventStore(persist_path=str(persist_path))
    assert {record.event.traceability_lot_code for record in reopened.recent(limit=10)} == {
        "TLC-DURABLE-1",
        "TLC-DURABLE-2",
    }


# --- #155: one Python definition of the default ingest endpoint -----------


def test_integration_status_serves_the_backend_default_endpoint():
    body = client.get("/api/integration/status").json()
    assert body["default_endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT


def test_default_ingest_endpoint_is_defined_once_in_python():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    hits = [
        path
        for path in list((repo_root / "app").rglob("*.py")) + list((repo_root / "scripts").rglob("*.py"))
        if DEFAULT_LIVE_INGEST_ENDPOINT in path.read_text(encoding="utf-8")
    ]
    assert hits == [repo_root / "app" / "regengine_client.py"]

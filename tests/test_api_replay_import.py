"""Replay of a persisted log and CSV import through the API.

Moved verbatim out of ``test_api.py`` (see #132).
"""

import asyncio
import base64
import json

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.simulation import SimulationConfig


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

    asyncio.run(controller.reset(SimulationConfig()))


def test_replay_current_persisted_log_posts_without_rewriting_records(tmp_path):
    custom_path = tmp_path / "replay-events.jsonl"
    reset_response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 2,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert reset_response.status_code == 200
    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200

    original_log = custom_path.read_text(encoding="utf-8")
    original_events = client.get("/api/events?limit=10").json()["events"]

    response = client.post("/api/simulate/replay")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["read"] == 2
    assert body["replayed"] == 2
    assert body["posted"] == 2
    assert body["failed"] == 0
    assert body["delivery_mode"] == "mock"
    assert body["persist_path"] == str(custom_path)
    assert body["response"]["total"] == 2

    assert custom_path.read_text(encoding="utf-8") == original_log
    assert client.get("/api/events?limit=10").json()["events"] == original_events


def test_replay_accepts_override_path_and_delivery_none(tmp_path):
    current_path = tmp_path / "current-events.jsonl"
    override_path = tmp_path / "override-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(current_path),
        },
    )
    client.post("/api/simulate/step")
    override_path.write_text(current_path.read_text(encoding="utf-8"), encoding="utf-8")
    original_override_log = override_path.read_text(encoding="utf-8")

    response = client.post(
        "/api/simulate/replay",
        json={
            "persist_path": str(override_path),
            "source": "replay-suite",
            "delivery": {"mode": "none"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rebuilt"
    assert body["read"] == 1
    assert body["replayed"] == 1
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["source"] == "replay-suite"
    assert body["delivery_mode"] == "none"
    assert body["persist_path"] == str(override_path)
    assert body["response"] is None
    assert override_path.read_text(encoding="utf-8") == original_override_log


def test_replay_missing_log_returns_empty_counts(tmp_path):
    missing_path = tmp_path / "missing-events.jsonl"

    response = client.post("/api/simulate/replay", json={"persist_path": str(missing_path)})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["read"] == 0
    assert body["replayed"] == 0
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["persist_path"] == str(missing_path)


def test_csv_import_scheduled_events_stores_valid_rows_and_reports_errors(tmp_path):
    custom_path = tmp_path / "csv-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    csv_text = """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,source_traceability_lot_code,kdes
harvesting,TLC-CSV-HARVEST,Romaine Lettuce,120,cases,Valley Fresh Farms,2026-02-05T08:00:00Z,,"{""harvest_date"":""2026-02-05""}"
initial_packing,TLC-CSV-PACKED,Romaine Lettuce,112,cases,Coastal Packhouse,2026-02-05T10:00:00Z,TLC-CSV-HARVEST,"{""pack_date"":""2026-02-05""}"
receiving,TLC-CSV-BAD,Romaine Lettuce,,cases,Distribution Center #4,2026-02-05T12:00:00Z,,
"""

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "scheduled_events",
            "csv_text": csv_text,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["total"] == 3
    assert body["accepted"] == 2
    assert body["rejected"] == 1
    assert body["stored"] == 2
    # Rows parse fine but are missing canonical KDEs (reference_document,
    # tlc_source_reference, ...), so the realistic mock — like live
    # RegEngine — rejects them at ingest instead of silently accepting.
    assert body["posted"] == 0
    assert body["failed"] == 2
    assert body["delivery_mode"] == "mock"
    assert body["errors"] == [
        {"row": 4, "field": "quantity", "message": "Missing required field: quantity"}
    ]
    assert {warning["field"] for warning in body["warnings"]} >= {
        "farm_location",
        "reference_document",
        "packing_date",
        "tlc_source_reference",
    }

    events = client.get("/api/events?limit=10").json()["events"]
    assert len(events) == 2
    assert events[0]["delivery_status"] == "failed"
    assert "Missing required KDEs" in events[0]["error"]
    assert events[0]["delivery_response"]["status"] == "rejected"
    assert events[0]["event"]["traceability_lot_code"] == "TLC-CSV-PACKED"
    assert events[0]["parent_lot_codes"] == ["TLC-CSV-HARVEST"]
    assert set(events[0]["event"]) == {
        "cte_type",
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "location_name",
        "location_gln",
        "timestamp",
        "kdes",
    }

    lineage_response = client.get("/api/lineage/TLC-CSV-PACKED").json()
    lineage = lineage_response["records"]
    assert [record["event"]["traceability_lot_code"] for record in lineage] == [
        "TLC-CSV-HARVEST",
        "TLC-CSV-PACKED",
    ]
    assert [node["lot_code"] for node in lineage_response["nodes"]] == [
        "TLC-CSV-HARVEST",
        "TLC-CSV-PACKED",
    ]
    assert lineage_response["edges"] == [
        {
            "source_lot_code": "TLC-CSV-HARVEST",
            "target_lot_code": "TLC-CSV-PACKED",
            "cte_type": "initial_packing",
            "event_sequence_no": 2,
        }
    ]


def test_csv_import_seed_lots_builds_harvesting_events_with_none_delivery(tmp_path):
    custom_path = tmp_path / "seed-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    csv_text = """traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,field_name,immediate_subsequent_recipient
TLC-SEED-001,Spinach,80,cases,Valley Fresh Farms,2026-02-06T09:15:00Z,Field-9,Central Coast Cooler
"""

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "seed_lots",
            "csv_text": csv_text,
            "source": "seed-suite",
            "delivery": {"mode": "none"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["accepted"] == 1
    assert body["rejected"] == 0
    assert body["posted"] == 0
    assert body["delivery_mode"] == "none"

    record = client.get("/api/events?limit=1").json()["events"][0]
    event = record["event"]
    assert record["payload_source"] == "seed-suite"
    assert record["delivery_status"] == "generated"
    assert event["cte_type"] == "harvesting"
    assert event["traceability_lot_code"] == "TLC-SEED-001"
    assert event["kdes"]["harvest_date"] == "2026-02-06"
    assert event["kdes"]["farm_location"] == "Valley Fresh Farms"
    assert event["kdes"]["field_name"] == "Field-9"
    assert event["kdes"]["reference_document_number"] == "CSV-TLC-SEED-001"
    assert event["kdes"]["tlc_source_reference"] == "CSV-SEED-TLC-SEED-001"

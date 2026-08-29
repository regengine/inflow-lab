"""The /api/import/csv route for both import types -- scheduled_events
(with per-row errors and warnings) and seed_lots (split out of
tests/test_api.py for #132).
"""

from datetime import timedelta

from tests.support.api_client import client, reset_app_state
from tests.support.timestamps import CANONICAL_EVENT_DATE, canonical_event_timestamp


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


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
    # Relative timestamps, not literals: the mock enforces RegEngine's
    # 90-day replay window, and a fixed date here would eventually add a
    # second, unrelated rejection reason on top of the missing-KDE one
    # this test is actually about (#209).
    harvest_at = canonical_event_timestamp()
    pack_at = canonical_event_timestamp(timedelta(hours=2))
    receive_at = canonical_event_timestamp(timedelta(hours=4))
    day = CANONICAL_EVENT_DATE
    csv_text = f"""cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,source_traceability_lot_code,kdes
harvesting,TLC-CSV-HARVEST,Romaine Lettuce,120,cases,Valley Fresh Farms,{harvest_at},,"{{""harvest_date"":""{day}""}}"
initial_packing,TLC-CSV-PACKED,Romaine Lettuce,112,cases,Coastal Packhouse,{pack_at},TLC-CSV-HARVEST,"{{""pack_date"":""{day}""}}"
receiving,TLC-CSV-BAD,Romaine Lettuce,,cases,Distribution Center #4,{receive_at},,
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
        "input_traceability_lot_codes",
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

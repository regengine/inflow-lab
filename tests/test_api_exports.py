"""FDA sortable-spreadsheet and EPCIS export surfaces.

Moved verbatim out of ``test_api.py`` (see #132).
"""

import asyncio
import base64
import csv
import io
import json

import pytest
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


def test_fda_export_shape_contains_expected_columns():
    client.post("/api/simulate/step")
    response = client.get("/api/mock/regengine/export/fda-request")
    assert response.status_code == 200
    csv_text = response.text
    assert "Traceability Lot Code" in csv_text
    assert "Location Identifier (GLN)" in csv_text
    assert "Reference Document Number" in csv_text


def parse_export_rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_fda_export_presets_filter_common_request_slices(tmp_path):
    custom_path = tmp_path / "fda-preset-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
            "delivery": {"mode": "none"},
        },
    )
    csv_text = """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,source_traceability_lot_code,input_traceability_lot_codes,reference_document_type,reference_document_number
harvesting,TLC-FDA-HARVEST,Romaine Lettuce,120,cases,Valley Fresh Farms,2026-02-05T08:00:00Z,,,Harvest Log,HAR-001
initial_packing,TLC-FDA-PACKED,Romaine Lettuce,112,cases,Coastal Packhouse,2026-02-05T10:00:00Z,TLC-FDA-HARVEST,,Packout Record,PACK-001
shipping,TLC-FDA-PACKED,Romaine Lettuce,112,cases,Coastal Packhouse,2026-02-05T12:00:00Z,,,Bill of Lading,BOL-001
receiving,TLC-FDA-PACKED,Romaine Lettuce,112,cases,Distribution Center #4,2026-02-05T18:00:00Z,,,Bill of Lading,BOL-001
transformation,TLC-FDA-OUT,Fresh Cut Salad Mix,95,cases,ReadyFresh Processing Plant,2026-02-06T09:00:00Z,,TLC-FDA-PACKED,Batch Record,BATCH-001
"""
    import_response = client.post(
        "/api/import/csv",
        json={
            "import_type": "scheduled_events",
            "csv_text": csv_text,
            "delivery": {"mode": "none"},
        },
    )
    assert import_response.status_code == 200
    assert import_response.json()["accepted"] == 5

    presets_response = client.get("/api/mock/regengine/export/presets")
    assert presets_response.status_code == 200
    preset_ids = [preset["id"] for preset in presets_response.json()["presets"]]
    assert preset_ids == [
        "all_records",
        "lot_trace",
        "shipment_handoff",
        "receiving_log",
        "transformation_batches",
    ]

    handoff_response = client.get("/api/mock/regengine/export/fda-request?preset=shipment_handoff")
    assert handoff_response.status_code == 200
    handoff_rows = parse_export_rows(handoff_response.text)
    assert [row["Traceability Lot Code Description"] for row in handoff_rows] == [
        "shipping",
        "receiving",
    ]
    assert handoff_rows[0]["Reference Document Number"] == "BOL-001"
    assert handoff_response.headers["content-disposition"] == (
        "attachment; filename=fda_request_shipment_handoff.csv"
    )

    trace_response = client.get(
        "/api/mock/regengine/export/fda-request?preset=lot_trace&traceability_lot_code=TLC-FDA-OUT"
    )
    assert trace_response.status_code == 200
    trace_rows = parse_export_rows(trace_response.text)
    assert [row["Traceability Lot Code"] for row in trace_rows] == [
        "TLC-FDA-HARVEST",
        "TLC-FDA-PACKED",
        "TLC-FDA-PACKED",
        "TLC-FDA-PACKED",
        "TLC-FDA-OUT",
    ]

    receiving_response = client.get("/api/mock/regengine/export/fda-request?preset=receiving_log")
    assert receiving_response.status_code == 200
    receiving_rows = parse_export_rows(receiving_response.text)
    assert [row["Traceability Lot Code Description"] for row in receiving_rows] == ["receiving"]

    transformation_response = client.get(
        "/api/mock/regengine/export/fda-request?preset=transformation_batches"
    )
    assert transformation_response.status_code == 200
    transformation_rows = parse_export_rows(transformation_response.text)
    assert [row["Traceability Lot Code"] for row in transformation_rows] == ["TLC-FDA-OUT"]

    missing_lot_response = client.get("/api/mock/regengine/export/fda-request?preset=lot_trace")
    assert missing_lot_response.status_code == 400


def test_epcis_export_scaffold_maps_lineage_to_jsonld_without_changing_ingest_contract(tmp_path):
    custom_path = tmp_path / "epcis-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
            "delivery": {"mode": "none"},
        },
    )
    load_response = client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={
            "source": "epcis-suite",
            "delivery": {"mode": "none"},
        },
    )
    assert load_response.status_code == 200

    response = client.get(
        "/api/mock/regengine/export/epcis?traceability_lot_code=TLC-DEMO-FC-OUT-001"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")
    assert response.headers["content-disposition"] == "attachment; filename=epcis_events.jsonld"
    document = response.json()
    assert document["type"] == "EPCISDocument"
    assert document["schemaVersion"] == "2.0"
    assert document["sender"] == "epcis-suite"

    events = document["epcisBody"]["eventList"]
    assert len(events) == 13
    assert {event["type"] for event in events} == {"ObjectEvent", "TransformationEvent"}
    assert events[0]["regengine:cteType"] == "harvesting"
    assert events[0]["quantityList"][0]["epcClass"] == "urn:regengine:lot:TLC-DEMO-FC-HARVEST-001"

    shipping_event = next(event for event in events if event["regengine:cteType"] == "shipping")
    assert shipping_event["bizStep"] == "urn:epcglobal:cbv:bizstep:shipping"
    assert shipping_event["disposition"] == "urn:epcglobal:cbv:disp:in_transit"
    assert shipping_event["bizTransactionList"][0]["regengine:documentNumber"]

    transformation_event = next(event for event in events if event["type"] == "TransformationEvent")
    assert transformation_event["transformationID"] == "urn:regengine:batch:BATCH-DEMO-FC-001"
    assert {
        quantity["regengine:traceabilityLotCode"]
        for quantity in transformation_event["inputQuantityList"]
    } == {"TLC-DEMO-FC-PACK-001", "TLC-DEMO-FC-PACK-002"}
    assert transformation_event["outputQuantityList"][0]["regengine:traceabilityLotCode"] == (
        "TLC-DEMO-FC-OUT-001"
    )
    assert transformation_event["regengine:kdes"]["reference_document_number"] == "BATCH-DEMO-FC-001"

    ingest_response = client.post(
        "/api/mock/regengine/ingest",
        json={
            "source": "contract-check",
            "events": [
                {
                    "cte_type": "receiving",
                    "traceability_lot_code": "TLC-EP-CHECK",
                    "product_description": "Romaine Lettuce",
                    "quantity": 12,
                    "unit_of_measure": "cases",
                    "location_name": "Distribution Center #4",
                    "timestamp": "2026-02-05T08:30:00Z",
                    "kdes": {
                        "receive_date": "2026-02-05",
                        "receiving_location": "Distribution Center #4",
                        "immediate_previous_source": "Coastal Packhouse",
                        "reference_document": "Bill of Lading BOL-EP-CHECK",
                        "tlc_source_reference": "SRC-EP-CHECK",
                    },
                }
            ],
        },
    )
    assert ingest_response.status_code == 200
    assert ingest_response.json()["events"][0]["status"] == "accepted"


def test_epcis_export_supports_date_filters_and_missing_lot_errors(tmp_path):
    custom_path = tmp_path / "epcis-date-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
            "delivery": {"mode": "none"},
        },
    )
    client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={"delivery": {"mode": "none"}},
    )

    filtered_response = client.get(
        "/api/mock/regengine/export/epcis?start_date=2026-02-07&end_date=2026-02-07"
    )
    assert filtered_response.status_code == 200
    filtered_events = filtered_response.json()["epcisBody"]["eventList"]
    assert [event["regengine:cteType"] for event in filtered_events] == ["receiving"]
    assert filtered_events[0]["regengine:traceabilityLotCode"] == "TLC-DEMO-FC-OUT-001"

    missing_response = client.get("/api/mock/regengine/export/epcis?traceability_lot_code=NOPE")
    assert missing_response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/mock/regengine/export/fda-request?start_date=banana",
        "/api/mock/regengine/export/fda-request?end_date=2026-99-99",
        "/api/mock/regengine/export/fda-request?start_date=2026-03-01&end_date=2026-02-01",
        "/api/mock/regengine/export/epcis?start_date=banana",
        "/api/mock/regengine/export/epcis?end_date=2026-99-99",
        "/api/mock/regengine/export/epcis?start_date=2026-03-01&end_date=2026-02-01",
    ],
)
def test_exports_reject_invalid_date_filters(path):
    response = client.get(path)

    assert response.status_code == 400

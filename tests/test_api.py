import asyncio
import csv
import io
import json

from fastapi.testclient import TestClient

from app.main import app, controller, scenario_saves
from app.models import SimulationConfig
from app.scenarios import ScenarioId, get_scenario


client = TestClient(app)


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


def test_scenario_catalog_endpoint_lists_supported_presets():
    response = client.get("/api/scenarios")

    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert [scenario["id"] for scenario in scenarios] == [
        "leafy_greens_supplier",
        "fresh_cut_processor",
        "retailer_readiness_demo",
    ]
    assert all(scenario["label"] for scenario in scenarios)


def test_scenario_save_load_restores_config_and_event_log(tmp_path):
    scenario_saves.configure(str(tmp_path / "scenario-saves"))
    fresh_path = tmp_path / "fresh-cut-events.jsonl"
    retailer_path = tmp_path / "retailer-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "fresh_cut_processor",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(fresh_path),
            "delivery": {"mode": "none"},
        },
    )
    client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={
            "source": "scenario-save-suite",
            "delivery": {"mode": "none"},
        },
    )

    save_response = client.post("/api/scenario-saves/fresh_cut_processor")
    assert save_response.status_code == 200
    save_body = save_response.json()
    assert save_body["status"] == "saved"
    assert save_body["save"]["scenario"] == "fresh_cut_processor"
    assert save_body["save"]["record_count"] == 13
    assert save_body["config"]["source"] == "scenario-save-suite"
    assert save_body["config"]["delivery"]["mode"] == "none"

    list_response = client.get("/api/scenario-saves")
    assert list_response.status_code == 200
    assert [save["scenario"] for save in list_response.json()["saves"]] == ["fresh_cut_processor"]

    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "retailer_readiness_demo",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(retailer_path),
            "delivery": {"mode": "none"},
        },
    )
    client.post("/api/simulate/step")
    assert client.get("/api/simulate/status").json()["stats"]["total_records"] == 1

    load_response = client.post("/api/scenario-saves/fresh_cut_processor/load")
    assert load_response.status_code == 200
    load_body = load_response.json()
    assert load_body["status"] == "loaded"
    assert load_body["loaded_records"] == 13
    assert load_body["config"]["scenario"] == "fresh_cut_processor"
    assert load_body["config"]["persist_path"] == str(fresh_path)

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == "fresh_cut_processor"
    assert status["stats"]["total_records"] == 13
    assert status["stats"]["engine"]["scenario"] == "fresh_cut_processor"
    lineage = client.get("/api/lineage/TLC-DEMO-FC-OUT-001").json()
    assert len(lineage["records"]) == 13


def test_scenario_save_sanitizes_live_credentials_and_missing_load_returns_404(tmp_path):
    scenario_saves.configure(str(tmp_path / "scenario-saves"))
    response = client.post(
        "/api/scenario-saves/retailer_readiness_demo",
        json={
            "config": {
                "source": "live-suite",
                "scenario": "retailer_readiness_demo",
                "interval_seconds": 2,
                "batch_size": 2,
                "seed": 204,
                "persist_path": str(tmp_path / "live-events.jsonl"),
                "delivery": {
                    "mode": "live",
                    "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
                    "api_key": "secret-key",
                    "tenant_id": "tenant-123",
                },
            }
        },
    )

    assert response.status_code == 200
    delivery = response.json()["config"]["delivery"]
    assert delivery["mode"] == "mock"
    assert delivery["endpoint"] is None
    assert delivery["api_key"] is None
    assert delivery["tenant_id"] is None

    missing_response = client.post("/api/scenario-saves/leafy_greens_supplier/load")
    assert missing_response.status_code == 404


def test_demo_fixture_catalog_lists_supported_playbacks():
    response = client.get("/api/demo-fixtures")

    assert response.status_code == 200
    fixtures = response.json()["fixtures"]
    assert [fixture["id"] for fixture in fixtures] == [
        "leafy_greens_trace",
        "fresh_cut_transformation",
        "retailer_handoff",
    ]
    assert fixtures[1]["scenario"] == "fresh_cut_processor"
    assert fixtures[1]["event_count"] == 13
    assert "TLC-DEMO-FC-OUT-001" in fixtures[1]["lot_codes"]


def test_load_demo_fixture_resets_store_and_preserves_transformation_lineage(tmp_path):
    custom_path = tmp_path / "demo-fixture-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    client.post("/api/simulate/step")

    response = client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={
            "reset": True,
            "source": "fixture-suite",
            "delivery": {"mode": "none"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "loaded"
    assert body["fixture_id"] == "fresh_cut_transformation"
    assert body["scenario"] == "fresh_cut_processor"
    assert body["loaded"] == 13
    assert body["stored"] == 13
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["source"] == "fixture-suite"
    assert body["delivery_mode"] == "none"
    assert body["lot_codes"] == [
        "TLC-DEMO-FC-HARVEST-001",
        "TLC-DEMO-FC-HARVEST-002",
        "TLC-DEMO-FC-PACK-001",
        "TLC-DEMO-FC-PACK-002",
        "TLC-DEMO-FC-OUT-001",
    ]

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == "fresh_cut_processor"
    assert status["stats"]["total_records"] == 13
    assert status["stats"]["by_delivery_status"] == {"generated": 13}

    lineage = client.get("/api/lineage/TLC-DEMO-FC-OUT-001").json()
    lineage_lot_codes = {record["event"]["traceability_lot_code"] for record in lineage["records"]}
    assert {
        "TLC-DEMO-FC-HARVEST-001",
        "TLC-DEMO-FC-HARVEST-002",
        "TLC-DEMO-FC-PACK-001",
        "TLC-DEMO-FC-PACK-002",
        "TLC-DEMO-FC-OUT-001",
    } <= lineage_lot_codes
    assert {
        (edge["source_lot_code"], edge["target_lot_code"])
        for edge in lineage["edges"]
    } >= {
        ("TLC-DEMO-FC-PACK-001", "TLC-DEMO-FC-OUT-001"),
        ("TLC-DEMO-FC-PACK-002", "TLC-DEMO-FC-OUT-001"),
    }

    export_response = client.get(
        "/api/mock/regengine/export/fda-request?preset=lot_trace&traceability_lot_code=TLC-DEMO-FC-OUT-001"
    )
    assert export_response.status_code == 200
    assert "BATCH-DEMO-FC-001" in export_response.text


def test_load_demo_fixture_posts_to_mock_when_requested(tmp_path):
    custom_path = tmp_path / "demo-fixture-mock-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )

    response = client.post(
        "/api/demo-fixtures/retailer_handoff/load",
        json={
            "delivery": {"mode": "mock"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "loaded"
    assert body["loaded"] == 7
    assert body["posted"] == 7
    assert body["delivery_mode"] == "mock"
    assert body["delivery_attempts"] == 1
    assert body["response"]["total"] == 7

    record = client.get("/api/events?limit=1").json()["events"][0]
    assert record["delivery_status"] == "posted"
    assert record["delivery_attempts"] == 1
    assert record["delivery_response"]["sha256_hash"]


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
                    "kdes": {},
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


def test_start_applies_configured_persist_path_and_keeps_mock_default(tmp_path):
    custom_path = tmp_path / "start-events.jsonl"
    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
            }
        },
    )
    try:
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["persist_path"] == str(custom_path)
        assert body["config"]["delivery"]["mode"] == "mock"
        assert body["stats"]["persist_path"] == str(custom_path)
    finally:
        client.post("/api/simulate/stop")


def test_reset_applies_configured_persist_path_for_next_step(tmp_path):
    custom_path = tmp_path / "reset-events.jsonl"
    response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["persist_path"] == str(custom_path)
    assert status["config"]["delivery"]["mode"] == "mock"
    assert status["stats"]["persist_path"] == str(custom_path)

    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200
    assert step_response.json()["generated"] == 1
    assert custom_path.exists()
    assert len(custom_path.read_text(encoding="utf-8").splitlines()) == 1


def test_failed_live_delivery_surfaces_retry_feedback_and_can_retry_to_mock(tmp_path):
    custom_path = tmp_path / "failed-delivery-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
            "delivery": {"mode": "live"},
        },
    )

    step_response = client.post("/api/simulate/step")

    assert step_response.status_code == 200
    step_body = step_response.json()
    assert step_body["generated"] == 1
    assert step_body["posted"] == 0
    assert step_body["failed"] == 1
    assert step_body["delivery_status"] == "failed"
    assert step_body["delivery_mode"] == "live"
    assert step_body["delivery_attempts"] == 1
    assert "api_key" in step_body["error"]

    status = client.get("/api/simulate/status").json()
    assert status["stats"]["delivery"]["failed"] == 1
    assert status["stats"]["delivery"]["retryable"] == 1
    assert status["stats"]["delivery"]["attempts"] == 1
    assert "api_key" in status["stats"]["delivery"]["last_error"]

    failed_record = client.get("/api/events?limit=1").json()["events"][0]
    assert failed_record["delivery_status"] == "failed"
    assert failed_record["delivery_attempts"] == 1
    assert failed_record["last_delivery_attempt_at"]
    assert failed_record["last_delivery_success_at"] is None

    retry_response = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}})

    assert retry_response.status_code == 200
    retry_body = retry_response.json()
    assert retry_body["status"] == "posted"
    assert retry_body["requested"] == 1
    assert retry_body["retryable"] == 1
    assert retry_body["attempted"] == 1
    assert retry_body["posted"] == 1
    assert retry_body["failed"] == 0
    assert retry_body["delivery_mode"] == "mock"
    assert retry_body["record_ids"] == [failed_record["record_id"]]

    events = client.get("/api/events?limit=10").json()["events"]
    assert len(events) == 1
    retried_record = events[0]
    assert retried_record["record_id"] == failed_record["record_id"]
    assert retried_record["delivery_status"] == "posted"
    assert retried_record["destination_mode"] == "mock"
    assert retried_record["delivery_attempts"] == 2
    assert retried_record["last_delivery_success_at"]
    assert retried_record["error"] is None


def test_delivery_retry_empty_when_no_failed_records():
    response = client.post("/api/delivery/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["requested"] == 0
    assert body["retryable"] == 0
    assert body["attempted"] == 0
    assert body["record_ids"] == []


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
    assert body["posted"] == 2
    assert body["delivery_mode"] == "mock"
    assert body["errors"] == [
        {"row": 4, "field": "quantity", "message": "Missing required field: quantity"}
    ]

    events = client.get("/api/events?limit=10").json()["events"]
    assert len(events) == 2
    assert events[0]["event"]["traceability_lot_code"] == "TLC-CSV-PACKED"
    assert events[0]["parent_lot_codes"] == ["TLC-CSV-HARVEST"]
    assert set(events[0]["event"]) == {
        "cte_type",
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "location_name",
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

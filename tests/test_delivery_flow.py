"""Delivery, run state, storage and import/export data flow.

Covers the flow-team issues: retry idempotency (#95), batch chunking (#103),
start-while-running engine state (#96), store read resilience (#93),
non-finite quantities (#98), FDA column labelling (#94), seed-lot lineage
(#99) and credential preservation on the config-replacing endpoints (#148).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.controller import _retry_idempotency_key
from app.csv_importer import parse_csv_import
from app.delivery import chunk_events, deliver_payload
from app.fda_export import FDA_EXPORT_COLUMNS, render_fda_request_csv
from app.main import app, controller
from app.mock_service import MAX_BATCH_EVENTS, MockRegEngineService
from app.regengine_client import LiveIngestResult
from app.schemas.domain import (
    CSVImportType,
    CTEType,
    DestinationMode,
    RegEngineEvent,
    StoredEventRecord,
)
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import LINEAGE_PLACEHOLDER_DESCRIPTION, EventStore


client = TestClient(app)

LIVE_DELIVERY = {
    "mode": "live",
    "api_key": "flow-api-secret",
    "tenant_id": "flow-tenant-secret",
}


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def teardown_function() -> None:
    # Credentials now survive a live-mode reconfigure, so every test that
    # configures live delivery hands the shared controller back in sandbox
    # mode rather than leaving a key behind for the next module.
    asyncio.run(controller.reset(SimulationConfig()))


def now_iso() -> str:
    """A timestamp inside the mock's replay window."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SEED_LOT_HEADER = (
    "traceability_lot_code,product_description,quantity,unit_of_measure,"
    "location_name,location_gln,timestamp,field_name,"
    "immediate_subsequent_recipient,kde_reference_document,kde_packaging_hierarchy"
)


def seed_lot_csv(count: int, prefix: str = "TLC-FLOW") -> str:
    timestamp = now_iso()
    rows = [
        f"{prefix}-{index:04d},Spinach,80,cases,Valley Fresh Farms,0123456789012,"
        f"{timestamp},Field-9,Central Coast Cooler,HARV-{index:04d},case"
        for index in range(count)
    ]
    return "\n".join([SEED_LOT_HEADER, *rows]) + "\n"


def harvest_event(lot_code: str) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot_code,
        product_description="Spinach",
        quantity=80,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=datetime.now(UTC),
        kdes={
            "harvest_date": datetime.now(UTC).date().isoformat(),
            "reference_document": f"HARV-{lot_code}",
            "field_name": "Field-9",
            "tlc_source_reference": f"CSV-SEED-{lot_code}",
        },
    )


# --- #95 retry idempotency -------------------------------------------------


class _RejectThenAcceptLiveClient:
    """2xx with a per-event rejection first, then a clean acceptance.

    This is the ordinary rejection shape (not a transport failure): the batch
    POST succeeds and the *event* is refused, which is the case retry exists
    to recover from.
    """

    def __init__(self) -> None:
        self.idempotency_keys: list[str | None] = []

    async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
        self.idempotency_keys.append(idempotency_key)
        rejected = len(self.idempotency_keys) == 1
        return LiveIngestResult(
            response={
                "accepted": 0 if rejected else len(payload.events),
                "rejected": len(payload.events) if rejected else 0,
                "total": len(payload.events),
                "events": [
                    {
                        "traceability_lot_code": event.traceability_lot_code,
                        "cte_type": event.cte_type.value,
                        "status": "rejected" if rejected else "accepted",
                        "errors": ["Missing required KDEs: reference_document"]
                        if rejected
                        else [],
                    }
                    for event in payload.events
                ],
            },
            metadata={
                "delivery_mode": "live",
                "endpoint_host": "www.regengine.co",
                "idempotency_key": idempotency_key,
                "status_code": 200,
            },
        )


def test_retry_of_a_per_event_rejection_mints_a_fresh_idempotency_key(tmp_path):
    """#95: the rejected event is re-sent under a new key and recovers."""
    fake_client = _RejectThenAcceptLiveClient()
    original_live_client = controller.live_client
    controller.live_client = fake_client
    try:
        reset = client.post(
            "/api/simulate/reset",
            json={
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(tmp_path / "retry-rejection.jsonl"),
                "delivery": LIVE_DELIVERY,
            },
        )
        assert reset.status_code == 200

        step = client.post("/api/simulate/step")
        assert step.status_code == 200
        record = client.get("/api/events?limit=1").json()["events"][0]
        # A per-event rejection on a 2xx: the batch was delivered, the event
        # was not, and the record carries the verdict.
        assert record["delivery_status"] == "failed"
        assert record["delivery_response"]["status"] == "rejected"
        original_key = record["delivery_metadata"]["idempotency_key"]

        retry = client.post("/api/delivery/retry")
    finally:
        controller.live_client = original_live_client

    assert retry.status_code == 200
    body = retry.json()
    assert body["status"] == "posted"
    # The retry's own accepted count, not the original batch's.
    assert body["posted"] == 1
    assert body["failed"] == 0

    retry_key = fake_client.idempotency_keys[1]
    assert retry_key is not None
    assert retry_key != original_key

    retried = client.get("/api/events?limit=1").json()["events"][0]
    assert retried["delivery_status"] == "posted"
    assert retried["error"] is None
    assert retried["delivery_metadata"]["idempotency_key"] == retry_key


def test_retry_key_is_reused_only_when_the_attempt_produced_no_verdict():
    """#95: replay-safety is kept exactly where it protects something."""
    event = harvest_event("TLC-FLOW-KEY")
    metadata = {"idempotency_key": "original-key"}

    transport_failure = StoredEventRecord(
        payload_source="flow",
        event=event,
        destination_mode=DestinationMode.LIVE,
        delivery_status="failed",
        delivery_response=None,
        delivery_metadata=metadata,
    )
    per_event_rejection = StoredEventRecord(
        payload_source="flow",
        event=event,
        destination_mode=DestinationMode.LIVE,
        delivery_status="failed",
        delivery_response={"status": "rejected", "errors": ["Missing required KDEs"]},
        delivery_metadata=metadata,
    )

    # No response at all: the original request may have landed, so replaying
    # the key is what stops a double ingest.
    assert _retry_idempotency_key(transport_failure) == "original-key"
    # A verdict came back, so the retry is a new request and needs a new key.
    assert _retry_idempotency_key(per_event_rejection) is None


def test_an_idempotency_replay_is_reported_instead_of_counted_as_posted():
    """#95: a cached answer never contributes another batch's accepted count."""
    service = MockRegEngineService()
    config = SimulationConfig(delivery=DeliveryConfig(mode=DestinationMode.MOCK))
    first_payload = IngestPayload(
        source="flow",
        events=[harvest_event("TLC-FLOW-R1"), harvest_event("TLC-FLOW-R2")],
    )
    subset_payload = IngestPayload(source="flow", events=[harvest_event("TLC-FLOW-R2")])

    first = asyncio.run(
        deliver_payload(
            first_payload,
            config,
            mock_service=service,
            live_client=None,
            idempotency_key="shared-key",
        )
    )
    assert first.delivery_status == "posted"
    assert first.posted == 2

    replayed = asyncio.run(
        deliver_payload(
            subset_payload,
            config,
            mock_service=service,
            live_client=None,
            idempotency_key="shared-key",
        )
    )

    assert replayed.delivery_status == "failed"
    assert replayed.posted == 0
    assert replayed.failed == 1
    assert replayed.metadata["idempotency_replay"] is True
    assert "idempotency cache" in replayed.error_message


# --- #103 batch chunking ---------------------------------------------------


def test_chunk_events_slices_at_the_regengine_batch_cap():
    events = list(range(MAX_BATCH_EVENTS + 100))
    chunks = chunk_events(events)
    assert [len(chunk) for chunk in chunks] == [MAX_BATCH_EVENTS, 100]
    assert [item for chunk in chunks for item in chunk] == events
    with pytest.raises(ValueError):
        chunk_events(events, 0)


def test_csv_import_above_the_batch_cap_is_chunked_and_fully_delivered(tmp_path):
    """#103: 600 rows used to 422 wholesale; they now go out in two requests."""
    row_count = MAX_BATCH_EVENTS + 100
    reset = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(tmp_path / "chunked-import.jsonl"),
        },
    )
    assert reset.status_code == 200

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "seed_lots",
            "csv_text": seed_lot_csv(row_count, prefix="TLC-CHUNK"),
            "delivery": {"mode": "mock"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["accepted"] == row_count
    assert body["stored"] == row_count
    assert body["posted"] == row_count
    assert body["failed"] == 0
    assert body["response"]["chunks"] == 2
    assert body["response"]["accepted"] == row_count

    stats = client.get("/api/simulate/status").json()["stats"]
    assert stats["total_records"] == row_count
    assert stats["by_delivery_status"] == {"posted": row_count}

    # `/api/events` caps at 500; the newest 500 span both chunks.
    events = client.get("/api/events?limit=500").json()["events"]
    assert len(events) == 500
    # Each chunk carried its own key, and verdicts stayed attached to the
    # records of the chunk that carried them.
    keys = {event["delivery_metadata"]["idempotency_key"] for event in events}
    assert len(keys) == 2
    assert all(
        event["delivery_response"]["traceability_lot_code"]
        == event["event"]["traceability_lot_code"]
        for event in events
    )


def test_replay_of_a_store_above_the_batch_cap_completes(tmp_path):
    """#103: replay is the steady state on a volume-backed deployment."""
    row_count = MAX_BATCH_EVENTS + 20
    persist_path = tmp_path / "chunked-replay.jsonl"
    client.post(
        "/api/simulate/reset",
        json={"batch_size": 1, "seed": 204, "persist_path": str(persist_path)},
    )
    seeded = client.post(
        "/api/import/csv",
        json={
            "import_type": "seed_lots",
            "csv_text": seed_lot_csv(row_count, prefix="TLC-REPLAY"),
            "delivery": {"mode": "none"},
        },
    )
    assert seeded.json()["stored"] == row_count

    response = client.post("/api/simulate/replay")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["read"] == row_count
    assert body["replayed"] == row_count
    assert body["posted"] == row_count
    assert body["failed"] == 0
    assert body["response"]["chunks"] == 2


# --- #96 start() on a running simulation -----------------------------------


def test_start_with_a_new_scenario_resets_the_engine_it_reports(tmp_path):
    """#96: config.scenario and stats.engine.scenario can never disagree."""
    started = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 204,
                "scenario": "leafy_greens_supplier",
                "persist_path": str(tmp_path / "start-scenario.jsonl"),
            }
        },
    )
    try:
        assert started.status_code == 200
        assert started.json()["stats"]["engine"]["scenario"] == "leafy_greens_supplier"

        restarted = client.post(
            "/api/simulate/start",
            json={
                "config": {
                    "interval_seconds": 0.01,
                    "batch_size": 1,
                    "seed": 204,
                    "scenario": "dairy_continuous_flow",
                    "persist_path": str(tmp_path / "start-scenario.jsonl"),
                }
            },
        )
        assert restarted.status_code == 200
        body = restarted.json()
        assert body["running"] is True
        assert body["config"]["scenario"] == "dairy_continuous_flow"
        assert body["stats"]["engine"]["scenario"] == "dairy_continuous_flow"

        status = client.get("/api/simulate/status").json()
        assert status["config"]["scenario"] == status["stats"]["engine"]["scenario"]
    finally:
        client.post("/api/simulate/stop")


def test_start_can_retune_pacing_on_a_running_simulation(tmp_path):
    """#96: batch_size/interval stay live-adjustable and do not reset."""
    persist_path = tmp_path / "start-pacing.jsonl"
    config = {
        "interval_seconds": 0.01,
        "batch_size": 1,
        "seed": 204,
        "scenario": "leafy_greens_supplier",
        "persist_path": str(persist_path),
    }
    started = client.post("/api/simulate/start", json={"config": config})
    try:
        assert started.status_code == 200
        engine_before = started.json()["stats"]["engine"]

        retuned = client.post(
            "/api/simulate/start",
            json={"config": {**config, "batch_size": 5, "interval_seconds": 0.05}},
        )
        assert retuned.status_code == 200
        body = retuned.json()
        assert body["running"] is True
        assert body["config"]["batch_size"] == 5
        # Same engine, still running: retuning pacing is not a reset.
        assert body["stats"]["engine"]["scenario"] == engine_before["scenario"]
        assert body["stats"]["engine"]["scale"] == engine_before["scale"]
        assert body["config"]["scenario"] == body["stats"]["engine"]["scenario"]
    finally:
        client.post("/api/simulate/stop")


# --- #93 store read resilience ---------------------------------------------


def test_store_skips_and_quarantines_malformed_lines(tmp_path):
    """#93: one bad line must not take out the tenant's whole history."""
    persist_path = tmp_path / "corrupt.jsonl"
    good = EventStore(persist_path=str(persist_path))
    good.add_many(
        [
            StoredEventRecord(payload_source="flow", event=harvest_event("TLC-GOOD-1")),
            StoredEventRecord(payload_source="flow", event=harvest_event("TLC-GOOD-2")),
        ]
    )
    with persist_path.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
        # A torn final line, as an interrupted append leaves behind.
        handle.write('{"payload_source": "flow", "event": {"cte_ty')

    reloaded = EventStore(persist_path=str(persist_path))

    assert [record.event.traceability_lot_code for record in reloaded.all_between()] == [
        "TLC-GOOD-1",
        "TLC-GOOD-2",
    ]
    assert reloaded.last_read_skipped == 2
    # Skipped, never silently dropped: both lines are set aside verbatim.
    quarantine = persist_path.with_suffix(".jsonl.quarantine").read_text(encoding="utf-8")
    assert "this is not json" in quarantine
    assert quarantine.count("\n") == 2
    # And the readable records are still writable/readable afterwards.
    reloaded.add_many([StoredEventRecord(payload_source="flow", event=harvest_event("TLC-GOOD-3"))])
    assert len(reloaded.all_between()) == 3


def test_a_corrupt_store_still_serves_reads_through_the_api(tmp_path):
    """#93: status/events keep answering instead of 400ing the whole tenant."""
    persist_path = tmp_path / "corrupt-api.jsonl"
    client.post(
        "/api/simulate/reset",
        json={"batch_size": 1, "seed": 204, "persist_path": str(persist_path)},
    )
    assert client.post("/api/simulate/step").status_code == 200
    with persist_path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all}\n")

    # Re-point the store at the same file so it is re-read from disk.
    reconfigured = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 205,
                "persist_path": str(persist_path),
            }
        },
    )
    try:
        assert reconfigured.status_code == 200
        assert client.get("/api/simulate/status").status_code == 200
        assert client.get("/api/events?limit=10").status_code == 200
    finally:
        client.post("/api/simulate/stop")


# --- #98 non-finite quantities ---------------------------------------------


@pytest.mark.parametrize("raw_quantity", ["nan", "inf", "-inf", "1e400"])
def test_csv_import_rejects_non_finite_quantities(raw_quantity, tmp_path):
    """#98: nan/inf used to pass every validator and reach durable JSONL."""
    persist_path = tmp_path / f"nonfinite-{raw_quantity}.jsonl"
    client.post(
        "/api/simulate/reset",
        json={"batch_size": 1, "seed": 204, "persist_path": str(persist_path)},
    )
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
        f"TLC-NONFINITE,Spinach,{raw_quantity},cases,Valley Fresh Farms\n"
    )

    response = client.post(
        "/api/import/csv",
        json={"import_type": "seed_lots", "csv_text": csv_text, "delivery": {"mode": "none"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["accepted"] == 0
    assert body["stored"] == 0
    assert body["errors"] == [
        {"row": 2, "field": "quantity", "message": "Quantity must be a finite number"}
    ]
    assert not persist_path.exists() or persist_path.read_text(encoding="utf-8") == ""


def test_scheduled_event_import_rejects_non_finite_quantities():
    """#98: both import types share the one quantity parser."""
    csv_text = (
        "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,timestamp\n"
        f"harvesting,TLC-NONFINITE,Spinach,nan,cases,Valley Fresh Farms,{now_iso()}\n"
    )

    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.events == []
    assert [(error.field, error.message) for error in parsed.errors] == [
        ("quantity", "Quantity must be a finite number")
    ]


def test_store_refuses_to_write_a_non_finite_quantity(tmp_path):
    """#98 writer boundary: no line lands that is not strict RFC 8259 JSON."""
    persist_path = tmp_path / "strict-json.jsonl"
    store = EventStore(persist_path=str(persist_path))
    event = harvest_event("TLC-NAN")
    event.quantity = math.nan

    with pytest.raises(ValueError):
        store.add_many([StoredEventRecord(payload_source="flow", event=event)])

    store.add_many([StoredEventRecord(payload_source="flow", event=harvest_event("TLC-OK"))])
    for line in persist_path.read_text(encoding="utf-8").splitlines():
        # `json.loads` accepts NaN by default; parse_constant makes it strict.
        json.loads(line, parse_constant=lambda token: pytest.fail(f"non-JSON token {token}"))


# --- #99 seed-lot lineage --------------------------------------------------


def test_seed_lot_import_keeps_parent_lot_codes(tmp_path):
    """#99: the column was stripped as a control field and then ignored."""
    persist_path = tmp_path / "seed-parents.jsonl"
    client.post(
        "/api/simulate/reset",
        json={"batch_size": 1, "seed": 204, "persist_path": str(persist_path)},
    )
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,parent_lot_codes\n"
        "TLC-SEED-CHILD,Spinach,80,cases,Valley Fresh Farms,TLC-SEED-A|TLC-SEED-B\n"
    )

    response = client.post(
        "/api/import/csv",
        json={"import_type": "seed_lots", "csv_text": csv_text, "delivery": {"mode": "none"}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    record = client.get("/api/events?limit=1").json()["events"][0]
    assert record["parent_lot_codes"] == ["TLC-SEED-A", "TLC-SEED-B"]


# --- #94 FDA export column labelling ---------------------------------------


def test_fda_export_columns_carry_what_their_headers_promise():
    """#94: the CTE used to sit under "Traceability Lot Code Description"."""
    record = StoredEventRecord(payload_source="flow", event=harvest_event("TLC-FDA-LABEL"))
    csv_text = render_fda_request_csv([record], location_gln=lambda name: "0123456789012")
    reader = csv.DictReader(io.StringIO(csv_text))
    (row,) = reader

    assert reader.fieldnames == FDA_EXPORT_COLUMNS
    assert row["Traceability Lot Code Description"] == "Spinach"
    assert row["Product Description"] == "Spinach"
    assert row["Event Type (CTE)"] == "harvesting"


def test_fda_export_prefers_a_supplied_lot_code_description():
    event = harvest_event("TLC-FDA-DESC")
    event.kdes["traceability_lot_code_description"] = "Baby spinach, 24-count case"
    record = StoredEventRecord(payload_source="flow", event=event)

    csv_text = render_fda_request_csv([record], location_gln=lambda name: "0123456789012")
    (row,) = csv.DictReader(io.StringIO(csv_text))

    assert row["Traceability Lot Code Description"] == "Baby spinach, 24-count case"
    assert row["Product Description"] == "Spinach"


# --- #97 handover: a parent lot with no record of its own -------------------


def packing_event(lot_code: str) -> RegEngineEvent:
    event = harvest_event(lot_code)
    event.cte_type = CTEType.INITIAL_PACKING
    event.kdes["packing_date"] = datetime.now(UTC).date().isoformat()
    return event


def test_lineage_shows_a_declared_parent_that_has_no_record(tmp_path):
    """A producer gap must be a hole in the graph, not an absence from it."""
    store = EventStore(persist_path=str(tmp_path / "unbacked-parent.jsonl"))
    store.add_many(
        [
            StoredEventRecord(
                payload_source="flow",
                event=packing_event("TLC-CHILD"),
                parent_lot_codes=["TLC-NO-RECORD"],
            )
        ]
    )

    records = store.lineage("TLC-CHILD")
    edges = store.lineage_edges(records)
    nodes = store.lineage_nodes(records)

    # The edge used to be dropped entirely because its source had no record.
    assert [(edge.source_lot_code, edge.target_lot_code) for edge in edges] == [
        ("TLC-NO-RECORD", "TLC-CHILD")
    ]
    by_lot = {node.lot_code: node for node in nodes}
    assert set(by_lot) == {"TLC-NO-RECORD", "TLC-CHILD"}
    placeholder = by_lot["TLC-NO-RECORD"]
    assert placeholder.event_count == 0
    assert placeholder.cte_types == []
    assert placeholder.locations == []
    assert placeholder.product_description == LINEAGE_PLACEHOLDER_DESCRIPTION
    # A lot that does have records is unaffected.
    assert by_lot["TLC-CHILD"].event_count == 1


def test_lineage_placeholders_reach_the_api(tmp_path):
    persist_path = tmp_path / "unbacked-parent-api.jsonl"
    client.post(
        "/api/simulate/reset",
        json={"batch_size": 1, "seed": 204, "persist_path": str(persist_path)},
    )
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,parent_lot_codes\n"
        "TLC-API-CHILD,Spinach,80,cases,Valley Fresh Farms,TLC-API-MISSING\n"
    )
    imported = client.post(
        "/api/import/csv",
        json={"import_type": "seed_lots", "csv_text": csv_text, "delivery": {"mode": "none"}},
    )
    assert imported.json()["status"] == "accepted"

    body = client.get("/api/lineage/TLC-API-CHILD").json()

    assert [edge["source_lot_code"] for edge in body["edges"]] == ["TLC-API-MISSING"]
    placeholder = next(node for node in body["nodes"] if node["lot_code"] == "TLC-API-MISSING")
    assert placeholder["event_count"] == 0
    assert placeholder["product_description"] == LINEAGE_PLACEHOLDER_DESCRIPTION


# --- #148 credential preservation ------------------------------------------


def configure_live_credentials() -> None:
    response = client.post("/api/integration/configure", json=LIVE_DELIVERY)
    assert response.status_code == 200
    assert response.json()["api_key_configured"] is True


def test_start_without_an_api_key_keeps_the_stored_credential(tmp_path):
    """#148: a console tab never receives the key, so it cannot resend it."""
    configure_live_credentials()

    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(tmp_path / "preserve-start.jsonl"),
                "delivery": {"mode": "live", "api_key": None, "tenant_id": None},
            }
        },
    )
    try:
        # Previously a 400: the omitted credentials were treated as a request
        # to clear them, and live delivery then failed validation.
        assert response.status_code == 200
        status = client.get("/api/integration/status").json()
        assert status["mode"] == "live"
        assert status["api_key_configured"] is True
        assert status["tenant_configured"] is True
    finally:
        client.post("/api/simulate/stop")


def test_reset_without_an_api_key_keeps_the_stored_credential(tmp_path):
    configure_live_credentials()

    response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(tmp_path / "preserve-reset.jsonl"),
            "delivery": {"mode": "live"},
        },
    )

    assert response.status_code == 200
    status = client.get("/api/integration/status").json()
    assert status["api_key_configured"] is True
    assert status["tenant_configured"] is True


def test_retry_without_an_api_key_still_reaches_the_live_endpoint(tmp_path):
    """#148: a stale form must not silently reroute a live retry."""

    class _RecordingLiveClient:
        def __init__(self) -> None:
            self.configs: list[DeliveryConfig] = []

        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            self.configs.append(config.delivery)
            raise AssertionError("delivery deliberately fails")

    fake_client = _RecordingLiveClient()
    original_live_client = controller.live_client
    controller.live_client = fake_client
    try:
        client.post(
            "/api/simulate/reset",
            json={
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(tmp_path / "preserve-retry.jsonl"),
                "delivery": LIVE_DELIVERY,
            },
        )
        assert client.post("/api/simulate/step").json()["delivery_status"] == "failed"

        retry = client.post(
            "/api/delivery/retry",
            json={"delivery": {"mode": "live", "api_key": None, "tenant_id": None}},
        )
    finally:
        controller.live_client = original_live_client

    assert retry.status_code == 200
    assert retry.json()["delivery_mode"] == "live"
    # Both the step and the retry carried the stored credentials.
    assert [config.api_key for config in fake_client.configs] == [
        "flow-api-secret",
        "flow-api-secret",
    ]
    assert [config.tenant_id for config in fake_client.configs] == [
        "flow-tenant-secret",
        "flow-tenant-secret",
    ]


def test_demo_fixture_load_without_an_api_key_keeps_the_stored_credential():
    configure_live_credentials()

    response = client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={"delivery": {"mode": "live", "api_key": None}},
    )

    # The fixture's live delivery is attempted (and fails against the real
    # client's unreachable endpoint or succeeds) rather than being rejected
    # for missing credentials.
    assert response.status_code == 200
    assert client.get("/api/integration/status").json()["api_key_configured"] is True


def test_a_sandbox_config_still_clears_a_stored_live_credential(tmp_path):
    """Pointing the simulator back at the sandbox is how a key is dropped."""
    configure_live_credentials()

    response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(tmp_path / "clear-credentials.jsonl"),
            "delivery": {"mode": "mock"},
        },
    )

    assert response.status_code == 200
    status = client.get("/api/integration/status").json()
    assert status["mode"] == "mock"
    assert status["api_key_configured"] is False

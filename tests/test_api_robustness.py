from __future__ import annotations

import json

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.simulation import SimulationConfig


# Regression tests for #143 (reject unknown/misplaced request body fields),
# #144 (record_ids: [] must retry nothing, not everything), and #145 (bound
# lineage/export response sizes). Kept in their own file rather than added
# to an existing one -- this batch of fixes must not edit any existing test
# file (strict per-file ownership across parallel workstreams).

client = TestClient(app)

_BASE_TIME = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def setup_function() -> None:
    # app/main.py's `controller` is a module-level singleton shared by every
    # test file in the process (same convention as tests/test_api.py's own
    # setup_function) -- reset it before each test so records left behind by
    # another test, or an earlier test in this file, can't leak in.
    asyncio.run(controller.reset(SimulationConfig()))


def _make_stored_record(
    lot_code: str,
    *,
    cte_type: CTEType = CTEType.HARVESTING,
    minutes: int = 0,
    parent_lot_codes: list[str] | None = None,
    kdes: dict[str, Any] | None = None,
    delivery_status: str = "generated",
    error: str | None = None,
) -> StoredEventRecord:
    """Build a record straight into the store, bypassing the simulator loop.

    Mirrors tests/test_store.py's make_record helper. Kept local instead of
    imported: test files are independent of each other, and this one owns
    nothing in test_store.py.
    """
    return StoredEventRecord(
        payload_source="robustness-suite",
        event=RegEngineEvent(
            cte_type=cte_type,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=_BASE_TIME + timedelta(minutes=minutes),
            kdes=kdes or {},
        ),
        parent_lot_codes=parent_lot_codes or [],
        destination_mode=DestinationMode.NONE,
        delivery_status=delivery_status,
        error=error,
    )


# ---------------------------------------------------------------------------
# #143 -- unrecognized or misnested request body fields return 422 instead
# of being silently dropped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, body",
    [
        pytest.param(
            "/api/mock/regengine/ingest",
            {
                "source": "robustness-suite",
                "events": [
                    {
                        "cte_type": "harvesting",
                        "traceability_lot_code": "TLC-EXTRA-000001",
                        "product_description": "Romaine Lettuce",
                        "quantity": 100,
                        "unit_of_measure": "cases",
                        "location_name": "Valley Fresh Farms",
                        "timestamp": "2026-03-01T08:00:00Z",
                        "kdes": {},
                    }
                ],
                # Misspelled -- previously silently ignored, so a caller who
                # made this typo would get a 200 with their real intent
                # (extra events, a different field) dropped on the floor.
                "eventz": [],
            },
            id="ingest-misspelled-extra-field",
        ),
        pytest.param(
            "/api/delivery/retry",
            # "record_id" (singular) is not a field -- previously this
            # parsed as an empty request and retried every failed record,
            # the opposite of a caller trying to target one record.
            {"record_id": ["abc"]},
            id="delivery-retry-misspelled-record-ids",
        ),
        pytest.param(
            "/api/simulate/replay",
            {"persist_paths": "data/events.jsonl"},
            id="replay-misspelled-persist-path",
        ),
        pytest.param(
            "/api/import/csv",
            {
                "import_type": "scheduled_events",
                "csv_text": "cte_type,traceability_lot_code\n",
                "type": "scheduled_events",
            },
            id="csv-import-stray-field",
        ),
        pytest.param(
            "/api/scenario-saves/leafy_greens_supplier",
            # The exact failure shape #143 was filed over: a misnamed
            # wrapper key around a config override, silently no-opping to
            # hard-coded defaults instead of applying (or rejecting) it.
            {"configuration": {"source": "should-not-apply"}},
            id="scenario-save-misnamed-config-wrapper",
        ),
        pytest.param(
            "/api/demo-fixtures/leafy_greens_trace/load",
            {"restart": True},
            id="demo-fixture-load-misspelled-reset",
        ),
    ],
)
def test_unknown_field_in_request_body_returns_422(path: str, body: dict[str, Any]) -> None:
    response = client.post(path, json=body)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(error["type"] == "extra_forbidden" for error in errors)


def test_ingest_with_well_formed_body_still_succeeds() -> None:
    # Paired with the parametrized rejections above: forbidding extras must
    # not make a correctly-shaped request to the same endpoint any harder.
    response = client.post(
        "/api/mock/regengine/ingest",
        json={
            "source": "robustness-suite",
            "events": [
                {
                    "cte_type": "harvesting",
                    "traceability_lot_code": "TLC-ROBUST-000001",
                    "product_description": "Romaine Lettuce",
                    "quantity": 100,
                    "unit_of_measure": "cases",
                    "location_name": "Valley Fresh Farms",
                    "timestamp": "2026-03-01T08:00:00Z",
                    "kdes": {"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-ROBUST-001"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["events"][0]["status"] == "accepted"


# ---------------------------------------------------------------------------
# #144 -- record_ids: [] means "retry nothing"; omitting record_ids entirely
# still means "retry every failed record up to limit".
# ---------------------------------------------------------------------------


def test_retry_with_explicit_empty_record_ids_retries_nothing() -> None:
    failed = _make_stored_record(
        "TLC-RETRY-EMPTY-001",
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-EMPTY-001"},
        delivery_status="failed",
        error="temporary outage",
    )
    controller.store.add_many([failed])

    response = client.post(
        "/api/delivery/retry",
        json={"record_ids": [], "delivery": {"mode": "mock"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["requested"] == 0
    assert body["retryable"] == 0
    assert body["attempted"] == 0
    assert body["record_ids"] == []

    # The failed record must be untouched -- not silently retried.
    events = client.get("/api/events?limit=10").json()["events"]
    retried = next(event for event in events if event["record_id"] == failed.record_id)
    assert retried["delivery_status"] == "failed"
    assert retried["delivery_attempts"] == 0


def test_retry_with_omitted_record_ids_retries_all_failed_records() -> None:
    first = _make_stored_record(
        "TLC-RETRY-ALL-001",
        minutes=0,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-ALL-001"},
        delivery_status="failed",
        error="temporary outage",
    )
    second = _make_stored_record(
        "TLC-RETRY-ALL-002",
        minutes=1,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-ALL-002"},
        delivery_status="failed",
        error="temporary outage",
    )
    controller.store.add_many([first, second])

    # record_ids omitted entirely -- distinct from record_ids: [] above.
    response = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["requested"] == 2
    assert body["retryable"] == 2
    assert body["attempted"] == 2
    assert body["posted"] == 2
    assert set(body["record_ids"]) == {first.record_id, second.record_id}


def test_retry_with_nonempty_record_ids_still_filters_to_that_subset() -> None:
    # Guards against a fix that special-cases [] so broadly it breaks the
    # ordinary filtered-subset path.
    target = _make_stored_record(
        "TLC-RETRY-FILTER-TARGET",
        minutes=0,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-FILTER-001"},
        delivery_status="failed",
        error="temporary outage",
    )
    other = _make_stored_record(
        "TLC-RETRY-FILTER-OTHER",
        minutes=1,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-FILTER-002"},
        delivery_status="failed",
        error="temporary outage",
    )
    controller.store.add_many([target, other])

    response = client.post(
        "/api/delivery/retry",
        json={"record_ids": [target.record_id], "delivery": {"mode": "mock"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 1
    assert body["attempted"] == 1
    assert body["record_ids"] == [target.record_id]

    events = client.get("/api/events?limit=10").json()["events"]
    other_record = next(event for event in events if event["record_id"] == other.record_id)
    assert other_record["delivery_status"] == "failed"  # untouched


# ---------------------------------------------------------------------------
# #145 -- lineage and export endpoint response sizes are bounded.
# ---------------------------------------------------------------------------


def test_lineage_truncates_past_limit_and_signals_via_headers() -> None:
    records = [
        _make_stored_record("TLC-CHAIN-0", cte_type=CTEType.HARVESTING, minutes=0),
        _make_stored_record(
            "TLC-CHAIN-1", cte_type=CTEType.INITIAL_PACKING, minutes=1, parent_lot_codes=["TLC-CHAIN-0"]
        ),
        _make_stored_record(
            "TLC-CHAIN-2", cte_type=CTEType.COOLING, minutes=2, parent_lot_codes=["TLC-CHAIN-1"]
        ),
        _make_stored_record(
            "TLC-CHAIN-3", cte_type=CTEType.SHIPPING, minutes=3, parent_lot_codes=["TLC-CHAIN-2"]
        ),
        _make_stored_record(
            "TLC-CHAIN-4", cte_type=CTEType.RECEIVING, minutes=4, parent_lot_codes=["TLC-CHAIN-3"]
        ),
    ]
    controller.store.add_many(records)

    response = client.get("/api/lineage/TLC-CHAIN-4?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert len(body["records"]) == 2
    # An oldest-first prefix of the full match, not an arbitrary subset.
    assert [record["event"]["traceability_lot_code"] for record in body["records"]] == [
        "TLC-CHAIN-0",
        "TLC-CHAIN-1",
    ]
    assert response.headers["X-Lineage-Total-Matched"] == "5"
    assert response.headers["X-Lineage-Truncated"] == "true"

    # The GRAPH must not be truncated with the records. Building nodes/edges
    # from the prefix returned a structurally wrong answer rather than a
    # smaller one -- lineage_edges() drops an edge whose parent is outside the
    # set, so the newest half of the trace (where the food went, which is the
    # half a recall needs) disappeared entirely. This test previously asserted
    # nothing here, which is how that shipped.
    node_lot_codes = {node["lot_code"] for node in body["nodes"]}
    assert node_lot_codes == {f"TLC-CHAIN-{index}" for index in range(5)}

    # Most pointedly: the lot that was actually queried must appear in its own
    # lineage. With limit=2 it falls outside the record prefix entirely.
    assert "TLC-CHAIN-4" in node_lot_codes

    edge_pairs = {(edge["source_lot_code"], edge["target_lot_code"]) for edge in body["edges"]}
    assert edge_pairs == {
        ("TLC-CHAIN-0", "TLC-CHAIN-1"),
        ("TLC-CHAIN-1", "TLC-CHAIN-2"),
        ("TLC-CHAIN-2", "TLC-CHAIN-3"),
        ("TLC-CHAIN-3", "TLC-CHAIN-4"),
    }


def test_lineage_under_limit_is_not_marked_truncated() -> None:
    records = [
        _make_stored_record("TLC-SMALL-0", minutes=0),
        _make_stored_record("TLC-SMALL-1", minutes=1, parent_lot_codes=["TLC-SMALL-0"]),
    ]
    controller.store.add_many(records)

    response = client.get("/api/lineage/TLC-SMALL-1")

    assert response.status_code == 200
    assert len(response.json()["records"]) == 2
    assert response.headers["X-Lineage-Total-Matched"] == "2"
    assert response.headers["X-Lineage-Truncated"] == "false"


@pytest.mark.parametrize("limit", [0, 1001])
def test_lineage_limit_query_param_rejects_out_of_bounds_values(limit: int) -> None:
    # Query validation runs before the handler body, so this 422s even for a
    # lot code that does not exist.
    response = client.get(f"/api/lineage/TLC-DOES-NOT-EXIST?limit={limit}")

    assert response.status_code == 422


def test_events_endpoint_limit_still_refuses_past_its_existing_maximum() -> None:
    # /api/events already bounded its response before this batch of fixes
    # (Query(..., le=500)). Documented here alongside /api/lineage's new
    # bound so the "a bounded endpoint refuses past its maximum" behavior
    # this batch adds is proven for both endpoints in this router.
    response = client.get("/api/events?limit=501")

    assert response.status_code == 422


def test_wrapped_reset_body_is_rejected_instead_of_silently_ignored() -> None:
    """#143's headline repro: /api/simulate/reset validates the raw body as
    SimulationConfig, so a caller who wrapped it the way /start expects used
    to get a 200 and an unchanged scenario. It must be a 422."""
    response = client.post(
        "/api/simulate/reset",
        json={"config": {"scenario": "dairy_continuous_flow"}},
    )
    assert response.status_code == 422


def test_unknown_key_inside_inline_delivery_block_is_rejected() -> None:
    """DeliveryConfig arrives nested inside other request bodies, so a typo
    there has to fail too -- not just an unknown key at the top level."""
    response = client.post(
        "/api/simulate/reset",
        json={"delivery": {"mode": "mock", "endpiont": "https://example.test"}},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Scenario saves must be forward-compatible, and one bad file must not hide
# every other save.
# ---------------------------------------------------------------------------


def test_scenario_save_with_unknown_config_keys_still_loads(tmp_path) -> None:
    """`ScenarioSaveSnapshot` documents itself as deliberately permissive so a
    save file from another schema version loads instead of hard-failing -- but
    its nested `SimulationConfig`/`DeliveryConfig` set `extra="forbid"`, and
    those are the version-carrying parts of the file.

    So a save written by a version that added one config field did not
    harmlessly ignore it; the whole file failed to load.
    """
    from app.scenario_saves import ScenarioSaveStore
    from app.scenarios import ScenarioId

    store = ScenarioSaveStore(save_dir=str(tmp_path))
    payload = {
        "scenario": ScenarioId.LEAFY_GREENS_SUPPLIER.value,
        "config": {
            "source": "codex-simulator",
            "scenario": ScenarioId.LEAFY_GREENS_SUPPLIER.value,
            "a_field_a_future_version_added": True,
            "delivery": {"mode": "mock", "another_future_field": 7},
        },
        "records": [],
        "saved_at": "2026-02-05T08:30:00Z",
    }
    (tmp_path / f"{ScenarioId.LEAFY_GREENS_SUPPLIER.value}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    snapshot = store.get(ScenarioId.LEAFY_GREENS_SUPPLIER)
    assert snapshot is not None, "a save carrying an unknown config key failed to load"
    assert snapshot.config.source == "codex-simulator"
    assert snapshot.config.delivery.mode.value == "mock"


def test_one_unreadable_save_does_not_hide_the_others(tmp_path) -> None:
    """`list()` calls `get()` for every scenario id, so a single unparseable
    file used to turn the whole listing into a 400 -- the operator lost sight
    of saves that were perfectly fine.
    """
    from app.scenario_saves import ScenarioSaveStore
    from app.scenarios import ScenarioId

    store = ScenarioSaveStore(save_dir=str(tmp_path))
    ids = list(ScenarioId)
    good, bad = ids[0], ids[1]

    (tmp_path / f"{good.value}.json").write_text(
        json.dumps(
            {
                "scenario": good.value,
                "config": {"source": "codex-simulator", "scenario": good.value},
                "records": [],
                "saved_at": "2026-02-05T08:30:00Z",
            }
        ),
        encoding="utf-8",
    )
    # Not merely an unknown key -- genuinely unparseable, so the pruning
    # validator cannot rescue it and list() has to skip it.
    (tmp_path / f"{bad.value}.json").write_text("{not json at all", encoding="utf-8")

    saves = store.list()

    assert [snapshot.scenario for snapshot in saves] == [good], (
        "an unreadable save file hid the readable ones"
    )


# ---------------------------------------------------------------------------
# Ingest strictness belongs on the events, not only the envelope
# ---------------------------------------------------------------------------


def _ingest_event_body(**overrides) -> dict:
    event = {
        "cte_type": "harvesting",
        "traceability_lot_code": "TLC-INGEST-000001",
        "product_description": "Romaine Lettuce",
        "quantity": 100,
        "unit_of_measure": "cases",
        "location_name": "Valley Fresh Farms",
        "timestamp": "2026-02-05T08:30:00Z",
        "kdes": {},
    }
    event.update(overrides)
    return event


def test_ingest_rejects_a_misspelled_field_inside_an_event() -> None:
    """The envelope's extra="forbid" caught a misspelled "events" key but did
    nothing about a misspelled field INSIDE an event, because RegEngineEvent
    is permissive -- so `product_desc` was silently dropped and the event was
    ingested as though it were complete.

    That is the far likelier integration typo, and surfacing exactly that is
    what this simulator exists for.
    """
    response = client.post(
        "/api/mock/regengine/ingest",
        json={"source": "test", "events": [_ingest_event_body(product_desc="typo")]},
    )

    assert response.status_code == 422, (
        "a misspelled per-event field was accepted; it would be silently dropped"
    )
    assert "product_desc" in response.text


def test_ingest_accepts_the_body_level_tenant_id_the_contract_documents() -> None:
    """The contract reference states RegEngine "resolves tenant from body,
    then API-key lookup, then RBAC principal", so a body-level tenant id is
    part of the shape real RegEngine accepts. Refusing it made the mock
    stricter than the thing it simulates -- a parity break in the direction
    that produces false confidence.
    """
    response = client.post(
        "/api/mock/regengine/ingest",
        json={"source": "test", "tenant_id": "acme-tenant", "events": [_ingest_event_body()]},
    )

    assert response.status_code == 200, response.text


def test_ingest_still_rejects_a_misspelled_envelope_key() -> None:
    """#143's original protection must survive: a caller who misspells
    "events" gets a 422, not a silent 200 that ingested nothing."""
    response = client.post(
        "/api/mock/regengine/ingest",
        json={"source": "test", "event": [_ingest_event_body()]},
    )

    assert response.status_code == 422


def test_body_level_tenant_id_never_reaches_the_live_wire_payload() -> None:
    """IngestPayload also builds the OUTBOUND payload in
    LiveRegEngineClient.ingest, so a field that serialized would change the
    bytes sent to real RegEngine -- and the bytes the HMAC is computed over.
    Tenant goes to live RegEngine in the X-Tenant-ID header instead.
    """
    from app.schemas.ingestion import IngestPayload

    payload = IngestPayload.model_validate(
        {"source": "test", "tenant_id": "acme-tenant", "events": [_ingest_event_body()]}
    )

    assert payload.tenant_id == "acme-tenant", "the field must still be populated on input"
    assert "tenant_id" not in payload.model_dump(mode="json")


def test_ingest_route_still_documents_its_request_body() -> None:
    """The ingest route reads its body by hand so HMAC verification can
    precede parsing (#209), which takes the body out of FastAPI's sight --
    and a body FastAPI cannot see is one it cannot document. The schema is
    reattached via openapi_extra; this pins that it is there, that it is
    the IngestPayload contract, and that every model it references resolves
    in the document's own components (the generator drops $defs on the
    assumption that they do).
    """
    import json as _json
    import re as _re

    document = client.get("/openapi.json").json()
    operation = document["paths"]["/api/mock/regengine/ingest"]["post"]

    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["title"] == "IngestPayload"
    assert schema["additionalProperties"] is False, "#143's extra='forbid' must stay documented"
    assert set(schema["properties"]) == {"source", "tenant_id", "events"}

    referenced = set(_re.findall(r'"#/components/schemas/([^"]+)"', _json.dumps(schema)))
    assert referenced, "the events array should reference the event model"
    assert not referenced - set(document["components"]["schemas"]), (
        "requestBody references a schema the document does not define"
    )

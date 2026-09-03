"""Pins for the #165 refactor: the shared ``_build_stored_records`` helper.

``SimulationController.step``, ``.import_csv``, and ``.load_demo_fixture``
used to each rebuild the same block inline -- pair delivery responses via
``_pair_event_responses``, loop to build a ``StoredEventRecord`` per event
via ``_event_delivery_fields``, then persist. That loop is now one shared
module-level helper, ``_build_stored_records``, that all three call.

This is a pure refactor: the full existing suite (361 tests as of #165)
already pins the externally observable behavior of ``step``/``import_csv``/
``load_demo_fixture``/``retry_failed_delivery`` and continues to pass
unmodified. What is *not* already covered anywhere else is the specific
risk a copy-paste-to-shared-helper refactor introduces: a call site now
passing the wrong source/events/parent_lot_codes/delivery_mode into the
extracted helper, or the helper itself mis-indexing between events,
per-event lineage, and paired responses. The tests below target exactly
that -- both the helper directly and each call site's wiring into it.

``retry_failed_delivery`` deliberately does NOT use ``_build_stored_records``
(it patches existing records via ``model_copy`` and accumulates
``delivery_attempts`` rather than building fresh records -- see the
docstring on ``_build_stored_records`` in app/controller.py) and this
refactor does not touch it. One light test below pins that its
accumulate-not-reset behavior survives regardless, per the task's own
example of what could plausibly regress.

Kept in its own file rather than added to an existing test file -- strict
per-file ownership across parallel workstreams means existing test files
must not be edited.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.controller import DeliveryOutcome, _build_stored_records
from app.demo_fixtures import get_demo_fixture
from app.main import controller
from app.schemas.domain import (
    CSVImportType,
    CTEType,
    DemoFixtureId,
    DestinationMode,
    RegEngineEvent,
    StoredEventRecord,
)
from app.schemas.ingestion import CSVImportRequest, DeliveryRetryRequest
from app.schemas.scenarios import DemoFixtureLoadRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig


_BASE_TIME = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def _response(lot_code: str, cte_type: str, status: str, **extra) -> dict:
    return {
        "traceability_lot_code": lot_code,
        "cte_type": cte_type,
        "status": status,
        **extra,
    }


def _event(cte_type: CTEType, lot_code: str) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=cte_type,
        traceability_lot_code=lot_code,
        product_description="Romaine Lettuce",
        quantity=10,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=_BASE_TIME,
    )


# ---------------------------------------------------------------------------
# _build_stored_records itself: identity-based pairing and index-aligned
# parent_lot_codes, in one place, directly against the helper.
# ---------------------------------------------------------------------------


def test_build_stored_records_pairs_by_identity_and_threads_parent_lot_codes_by_index():
    """Rejected-first response, like real RegEngine returns for a mixed
    batch -- exactly the shape ``_pair_event_responses``'s own docstring
    describes. Each record must end up with its OWN event's parent_lot_codes
    and its OWN event's verdict, not a neighbour's, even though the
    non-rejected verdicts arrive out of request order and the
    parent_lot_codes carry different values (and lengths) per event.
    """
    events = [
        _event(CTEType.HARVESTING, "LOT-A"),
        _event(CTEType.COOLING, "LOT-B"),
        _event(CTEType.SHIPPING, "LOT-C"),
    ]
    parent_lot_codes = [["P-A"], [], ["P-C1", "P-C2"]]
    response_events = [
        _response("LOT-B", "cooling", "rejected", errors=["missing KDE: location_gln"]),
        _response("LOT-A", "harvesting", "accepted", event_id="evt-a", sha256_hash="sha-a"),
        _response("LOT-C", "shipping", "accepted", event_id="evt-c", sha256_hash="sha-c"),
    ]
    outcome = DeliveryOutcome(
        response={"accepted": 2, "rejected": 1, "events": response_events},
        delivery_status="posted",
        posted=2,
        failed=1,
        delivery_attempts=1,
        attempted_at=_BASE_TIME,
        completed_at=_BASE_TIME,
        metadata={"delivery_mode": "mock", "idempotency_key": "idem-1"},
    )

    records = _build_stored_records("src-refactor-test", events, parent_lot_codes, DestinationMode.MOCK, outcome)

    assert len(records) == 3
    a, b, c = records

    assert a.event.traceability_lot_code == "LOT-A"
    assert a.payload_source == "src-refactor-test"
    assert a.parent_lot_codes == ["P-A"]
    assert a.destination_mode == DestinationMode.MOCK
    assert a.delivery_attempts == 1
    assert a.last_delivery_attempt_at == _BASE_TIME
    assert a.last_delivery_success_at == _BASE_TIME
    assert a.delivery_status == "posted"
    assert a.error is None
    assert a.delivery_response is not None and a.delivery_response["event_id"] == "evt-a"
    assert a.delivery_metadata == {"delivery_mode": "mock", "idempotency_key": "idem-1"}

    assert b.event.traceability_lot_code == "LOT-B"
    assert b.parent_lot_codes == []
    assert b.delivery_status == "failed"
    assert b.last_delivery_success_at is None
    assert "missing KDE" in (b.error or "")
    assert b.delivery_response is not None and b.delivery_response["status"] == "rejected"

    assert c.event.traceability_lot_code == "LOT-C"
    assert c.parent_lot_codes == ["P-C1", "P-C2"]
    assert c.delivery_status == "posted"
    assert c.delivery_response is not None and c.delivery_response["event_id"] == "evt-c"


# ---------------------------------------------------------------------------
# step: engine-sourced events/lineages wired through the shared helper.
# ---------------------------------------------------------------------------


def test_step_wires_source_and_parent_lot_codes_through_shared_builder(monkeypatch):
    asyncio.run(controller.reset(SimulationConfig(source="step-refactor-src")))

    canned = iter(
        [
            (_event(CTEType.HARVESTING, "TLC-REFACTOR-STEP-A"), ["PARENT-A"]),
            (_event(CTEType.COOLING, "TLC-REFACTOR-STEP-B"), []),
        ]
    )

    def fake_next_event():
        return next(canned)

    monkeypatch.setattr(controller.engine, "next_event", fake_next_event)

    async def fake_deliver(payload, config, idempotency_key=None):
        sent = list(payload.events)
        # Rejected-first again: if step's call site mismatched
        # events/lineages/outcome going into the helper, this is what
        # would expose it.
        response_events = [
            _response(sent[1].traceability_lot_code, sent[1].cte_type.value, "rejected", errors=["missing KDE: x"]),
            _response(sent[0].traceability_lot_code, sent[0].cte_type.value, "accepted", event_id="evt-a"),
        ]
        return DeliveryOutcome(
            response={"accepted": 1, "rejected": 1, "events": response_events},
            delivery_status="posted",
            posted=1,
            failed=1,
            delivery_attempts=1,
            attempted_at=_BASE_TIME,
            completed_at=_BASE_TIME,
            metadata={"delivery_mode": "mock"},
        )

    monkeypatch.setattr(controller, "_deliver_payload", fake_deliver)

    asyncio.run(controller.step(batch_size=2))

    stored = {record.event.traceability_lot_code: record for record in controller.store.all_between()}

    assert stored["TLC-REFACTOR-STEP-A"].parent_lot_codes == ["PARENT-A"]
    assert stored["TLC-REFACTOR-STEP-A"].payload_source == "step-refactor-src"
    assert stored["TLC-REFACTOR-STEP-A"].destination_mode == DestinationMode.MOCK
    assert stored["TLC-REFACTOR-STEP-A"].delivery_status == "posted"
    assert stored["TLC-REFACTOR-STEP-A"].delivery_response["event_id"] == "evt-a"

    assert stored["TLC-REFACTOR-STEP-B"].parent_lot_codes == []
    assert stored["TLC-REFACTOR-STEP-B"].delivery_status == "failed"
    assert "missing KDE" in (stored["TLC-REFACTOR-STEP-B"].error or "")


# ---------------------------------------------------------------------------
# import_csv: CSV-parsed events/parent_lot_codes wired through the shared
# helper, plus the "zero parsed events" guard around it.
# ---------------------------------------------------------------------------


def test_import_csv_wires_parent_lot_codes_per_row_through_shared_builder():
    asyncio.run(controller.reset(SimulationConfig()))
    # delivery=NONE short-circuits _deliver_payload to a bare DeliveryOutcome
    # (see app/controller.py), keeping this test focused on CSV-parsing ->
    # shared-builder wiring rather than coupling it to mock validation rules.
    csv_text = """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,kde_input_traceability_lot_codes
transformation,TLC-REFACTOR-CSV-OUT-1,Fresh Cut Salad Mix,50,cases,ReadyFresh Processing Plant,2026-02-07T12:00:00Z,TLC-REFACTOR-CSV-IN-1|TLC-REFACTOR-CSV-IN-2
transformation,TLC-REFACTOR-CSV-OUT-2,Fresh Cut Salad Mix,75,cases,ReadyFresh Processing Plant,2026-02-07T13:00:00Z,TLC-REFACTOR-CSV-IN-3
"""
    request = CSVImportRequest(
        import_type=CSVImportType.SCHEDULED_EVENTS,
        csv_text=csv_text,
        delivery=DeliveryConfig(mode=DestinationMode.NONE),
    )

    response = asyncio.run(controller.import_csv(request))

    assert response.errors == []
    assert response.stored == 2
    stored = {record.event.traceability_lot_code: record for record in controller.store.all_between()}
    assert stored["TLC-REFACTOR-CSV-OUT-1"].parent_lot_codes == [
        "TLC-REFACTOR-CSV-IN-1",
        "TLC-REFACTOR-CSV-IN-2",
    ]
    assert stored["TLC-REFACTOR-CSV-OUT-2"].parent_lot_codes == ["TLC-REFACTOR-CSV-IN-3"]
    assert stored["TLC-REFACTOR-CSV-OUT-1"].payload_source == controller.config.source
    assert stored["TLC-REFACTOR-CSV-OUT-1"].destination_mode == DestinationMode.NONE


def test_import_csv_with_zero_parsed_events_stores_nothing():
    """import_csv only calls the shared builder (and _store_add_many) when
    parsed.events is non-empty; the refactor must not have made that call
    unconditional.
    """
    asyncio.run(controller.reset(SimulationConfig()))
    before = len(controller.store.all_between())

    response = asyncio.run(
        controller.import_csv(CSVImportRequest(import_type=CSVImportType.SEED_LOTS, csv_text=""))
    )

    assert response.accepted == 0
    assert response.stored == 0
    assert len(controller.store.all_between()) == before == 0


# ---------------------------------------------------------------------------
# load_demo_fixture: fixture-sourced events/parent_lot_codes wired through
# the shared helper.
# ---------------------------------------------------------------------------


def test_load_demo_fixture_wires_parent_lot_codes_per_fixture_event():
    asyncio.run(controller.reset(SimulationConfig()))
    fixture = get_demo_fixture(DemoFixtureId.LEAFY_GREENS_TRACE)

    asyncio.run(
        controller.load_demo_fixture(
            DemoFixtureId.LEAFY_GREENS_TRACE,
            DemoFixtureLoadRequest(delivery=DeliveryConfig(mode=DestinationMode.NONE)),
        )
    )

    stored = controller.store.all_between()
    assert len(stored) == len(fixture.events)
    # Position-paired, not joined by lot code: several fixture events below
    # share the same lot code, so only a positional zip against the
    # fixture's own declared order can catch an index shifted by one.
    for stored_record, fixture_event in zip(stored, fixture.events):
        assert stored_record.event.traceability_lot_code == fixture_event.event.traceability_lot_code
        assert stored_record.event.cte_type == fixture_event.event.cte_type
        # list(), not the source tuple -- app/controller.py converts
        # explicitly, and StoredEventRecord.parent_lot_codes is a list.
        assert stored_record.parent_lot_codes == list(fixture_event.parent_lot_codes)
        assert stored_record.destination_mode == DestinationMode.NONE


# ---------------------------------------------------------------------------
# retry_failed_delivery: untouched by this refactor (it does not use
# _build_stored_records -- see that helper's docstring), pinned anyway per
# the task's own example of a plausible regression to guard against.
# ---------------------------------------------------------------------------


def _make_failed_record(lot: str, *, attempts: int = 1) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="controller-refactor-suite",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=_BASE_TIME,
            kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-REFACTOR-001"},
        ),
        destination_mode=DestinationMode.NONE,
        delivery_status="failed",
        delivery_attempts=attempts,
        error="temporary outage",
    )


def test_retry_failed_delivery_still_accumulates_delivery_attempts_not_reset():
    asyncio.run(controller.reset(SimulationConfig()))
    controller.store.add_many([_make_failed_record("TLC-REFACTOR-RETRY-001", attempts=1)])

    response = asyncio.run(
        controller.retry_failed_delivery(DeliveryRetryRequest(delivery=DeliveryConfig(mode=DestinationMode.MOCK)))
    )

    assert response.status == "posted"
    stored = controller.store.all_between()
    assert len(stored) == 1
    # 1 prior attempt + 1 from this retry's outcome -- not reset to 1.
    assert stored[0].delivery_attempts == 2
    assert stored[0].delivery_status == "posted"


# ---------------------------------------------------------------------------
# Cross-call-site check: step/import_csv/load_demo_fixture all produce
# records satisfying the same shape invariants, exercised through the real
# mock delivery pipeline rather than a monkeypatched one.
# ---------------------------------------------------------------------------


def test_step_import_csv_and_load_demo_fixture_all_produce_shape_consistent_records():
    asyncio.run(controller.reset(SimulationConfig()))
    asyncio.run(controller.step(batch_size=2))
    asyncio.run(
        controller.import_csv(
            CSVImportRequest(
                import_type=CSVImportType.SEED_LOTS,
                csv_text=(
                    "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
                    "TLC-REFACTOR-SHAPE-001,Romaine Hearts,42,cases,Valley Fresh Farms\n"
                ),
            )
        )
    )
    asyncio.run(
        controller.load_demo_fixture(DemoFixtureId.LEAFY_GREENS_TRACE, DemoFixtureLoadRequest(reset=False))
    )

    records = controller.store.all_between()
    assert len(records) >= 4  # step's 2 + the CSV row + at least 1 fixture event
    for record in records:
        assert record.destination_mode == DestinationMode.MOCK
        assert record.delivery_attempts == 1
        if record.delivery_status == "posted":
            assert record.last_delivery_success_at is not None
            assert record.error is None
        else:
            assert record.delivery_status == "failed"
            assert record.last_delivery_success_at is None

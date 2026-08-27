"""Regression tests for issue #91 (transformation input-lot linkage on the
wire) and issue #97 (rework lots never emitting a CTE record of their own).

#91: RegEngine's live webhook route reads transformation input-lot linkage
only from a top-level ``input_traceability_lot_codes`` field on the
``IngestEvent`` it parses the wire body into
(``services/ingestion/app/webhook_models.py`` on RegEngine's side) --
never from ``kdes["input_traceability_lot_codes"]``, which is the only
place inflow-lab used to put it. See ``app/schemas/domain.py``'s
``RegEngineEvent`` and ``app/engine.py::_transform``.

#97: ``_transform`` used to mint a rework lot straight into
``processor_inventory`` without any CTE event of its own. When a later
transformation sampled it as an input, it showed up in that event's own
``input_traceability_lot_codes``/``parent_lot_codes``, but
``EventStore.lineage_edges`` (``app/store.py``) silently dropped the edge
because the rework lot's own code had no record anywhere to anchor to. See
``app/engine.py::_transform`` / ``next_event``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.engine import LegitFlowEngine
from app.mock_service import validate_event_like_regengine
from app.schemas.domain import CTEType, RegEngineEvent, StoredEventRecord
from app.schemas.ingestion import IngestPayload
from app.store import EventStore


SEED = 204
# fresh_cut_processor is the scenario the other engine-correctness suites
# already use to exercise multi-input transforms and rework
# (test_engine.py::test_transformation_can_emit_split_outputs_and_rework),
# and is also issue #91's own named repro scenario.
TRANSFORM_SCENARIO = "fresh_cut_processor"


def _first_multi_input_transformation(
    engine: LegitFlowEngine, attempts: int = 200
) -> tuple[RegEngineEvent, list[str]]:
    for _ in range(attempts):
        event, parents = engine.next_event()
        if event.cte_type == CTEType.TRANSFORMATION and len(parents) >= 2:
            return event, parents
    raise AssertionError(f"Expected a multi-input transformation event within {attempts} calls")


def _fingerprint(engine: LegitFlowEngine, count: int) -> list[tuple]:
    # Mirrors tests/test_engine_correctness.py's own determinism
    # fingerprint (timestamp deliberately excluded -- it is anchored to
    # wall-clock `now()`, not the seeded RNG), extended with the new
    # input_traceability_lot_codes field so this also proves *that* field
    # is a pure function of the seed, not just that older fields stayed
    # put.
    fingerprint = []
    for _ in range(count):
        event, parents = engine.next_event()
        fingerprint.append(
            (
                event.cte_type,
                event.product_description,
                event.quantity,
                event.unit_of_measure,
                event.location_name,
                event.location_gln,
                event.input_traceability_lot_codes,
                tuple(parents),
            )
        )
    return fingerprint


def _rework_lot_count(engine: LegitFlowEngine, count: int) -> int:
    total = 0
    for _ in range(count):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.TRANSFORMATION and event.kdes.get("rework_traceability_lot_codes"):
            total += 1
    return total


def _run_past_first_rework(
    engine: LegitFlowEngine, attempts: int = 250
) -> tuple[
    list[tuple[RegEngineEvent, list[str]]],
    tuple[RegEngineEvent, list[str]],
    tuple[RegEngineEvent, list[str]],
]:
    """Drive ``engine`` until a transformation reporting a rework lot has
    been observed, then keep going until that rework lot's own queued CTE
    record appears. Returns ``(collected, primary, rework)``.

    The primary event is still returned synchronously from the very call
    that performs the transformation. Its rework twin used to be the very
    next ``next_event()`` call, and this helper relied on that adjacency --
    but #115 now queues one record per *additional transformation output
    lot* on the same _pending_events queue, ahead of the rework one, so
    between 0 and 2 output records can sit in between. The rework record is
    located by lot code instead, which is what the callers actually mean and
    is independent of how many siblings the batch produced.
    """
    collected: list[tuple[RegEngineEvent, list[str]]] = []
    primary: tuple[RegEngineEvent, list[str]] | None = None
    for _ in range(attempts):
        collected.append(engine.next_event())
        event, _ = collected[-1]
        if event.cte_type != CTEType.TRANSFORMATION:
            continue
        if not event.kdes.get("rework_traceability_lot_codes"):
            continue
        if primary is None:
            primary = collected[-1]
            continue
        if event.traceability_lot_code in primary[0].kdes["rework_traceability_lot_codes"]:
            return collected, primary, collected[-1]
    raise AssertionError(f"Expected a transformation with a rework lot within {attempts} calls")


# ---------------------------------------------------------------------------
# #91 -- top-level input_traceability_lot_codes on the wire
# ---------------------------------------------------------------------------


def test_transformation_event_carries_top_level_input_lot_linkage():
    """Core #91 fix: the field RegEngine's IngestEvent actually reads is
    populated, top-level, with the transformation's real input lot codes."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    event, parents = _first_multi_input_transformation(engine)

    assert event.input_traceability_lot_codes == parents
    assert len(event.input_traceability_lot_codes) >= 2
    assert all(isinstance(code, str) and code for code in event.input_traceability_lot_codes)


def test_top_level_field_is_additive_not_a_replacement_for_the_kdes_copy():
    """Local lineage (app/store.py), EPCIS export (app/epcis_export.py), and
    the mock ingest validator (app/mock_service.py) all still read
    kdes["input_traceability_lot_codes"] -- #91's fix must add the
    top-level field alongside it, not instead of it."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    event, _ = _first_multi_input_transformation(engine)

    assert event.kdes["input_traceability_lot_codes"] == event.input_traceability_lot_codes


def test_wire_body_carries_the_field_at_top_level_not_nested_in_kdes():
    """Reproduces the exact serialization LiveRegEngineClient.ingest() sends
    (payload.model_dump(mode="json"), per app/regengine_client.py) and
    asserts the key RegEngine's IngestEvent reads is present as a top-level
    sibling of `kdes`, not only inside it."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    event, parents = _first_multi_input_transformation(engine)

    wire_body = IngestPayload(source="test-suite", events=[event]).model_dump(mode="json")
    wire_event = wire_body["events"][0]

    assert wire_event["input_traceability_lot_codes"] == parents
    assert wire_event["kdes"]["input_traceability_lot_codes"] == parents


def test_non_transformation_events_leave_the_new_field_unset():
    """The field only means something for Transformation; every other CTE
    type must leave it at its default so it never shows up on the wire for
    an event that has no input lots to begin with."""
    engine = LegitFlowEngine(seed=SEED)
    saw_a_non_transformation_event = False
    for _ in range(150):
        event, _ = engine.next_event()
        if event.cte_type != CTEType.TRANSFORMATION:
            saw_a_non_transformation_event = True
            assert event.input_traceability_lot_codes is None

    assert saw_a_non_transformation_event


def test_input_traceability_lot_codes_field_defaults_to_none():
    """Matches RegEngine's IngestEvent, which declares this field with a
    None default -- an empty-list default would send `[]` instead of the
    field being genuinely absent/null for events with no input lots, which
    is a different wire shape than RegEngine expects."""
    event = RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="TLC-TEST-000001",
        product_description="Test Product",
        quantity=1.0,
        unit_of_measure="cases",
        location_name="Test Farm",
        timestamp=datetime.now(tz=UTC),
    )
    assert event.input_traceability_lot_codes is None


def test_transformation_event_passes_the_mock_ingest_validator():
    """Explicit task requirement: adding the top-level field must not newly
    trip RegEngine's (mirrored) ingest validation."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    event, _ = _first_multi_input_transformation(engine)

    assert validate_event_like_regengine(event) == []


# ---------------------------------------------------------------------------
# Determinism -- the engine must stay seeded/reproducible (#117/#119
# preserved; the #97 pending-events queue must not add or reorder any
# self.rng draw)
# ---------------------------------------------------------------------------


def test_seeded_run_stays_reproducible_with_the_new_field_and_rework_queue():
    """Two fresh engines with the same seed must agree exactly, including
    the new input_traceability_lot_codes field and any events queued by
    #97's rework-record fix -- neither may depend on anything but the
    seeded RNG."""
    sanity_engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    assert _rework_lot_count(sanity_engine, 300) > 0, (
        "test scenario/seed/count must actually exercise the #97 "
        "pending-events queue for this to be a meaningful check"
    )

    first_run = _fingerprint(LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO), 300)
    second_run = _fingerprint(LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO), 300)

    assert first_run == second_run


def test_reset_with_same_seed_reproduces_the_same_run():
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    first_run = _fingerprint(engine, 200)

    engine.reset(seed=SEED, scenario=TRANSFORM_SCENARIO)
    second_run = _fingerprint(engine, 200)

    assert first_run == second_run


def test_next_event_always_returns_a_two_tuple_even_when_pending_queue_drains():
    """app/controller.py's step() unconditionally unpacks
    `event, parent_lot_codes = engine.next_event()` -- the #97 fix must
    never change that contract, whether a call is freshly generated or
    drained from the pending-events queue."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    for _ in range(200):
        result = engine.next_event()
        assert isinstance(result, tuple)
        assert len(result) == 2
        event, parents = result
        assert isinstance(event, RegEngineEvent)
        assert isinstance(parents, list)


def test_rework_event_timestamp_never_collides_with_or_precedes_its_primary():
    """#117/#119 must hold for the newly-queued rework event too: no two
    events may share a timestamp, and the rework lot's own record must
    still land strictly after the transformation that minted it."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    collected, (primary_event, _), (rework_event, _) = _run_past_first_rework(engine)

    assert primary_event.cte_type == CTEType.TRANSFORMATION
    assert rework_event.cte_type == CTEType.TRANSFORMATION
    assert rework_event.traceability_lot_code in primary_event.kdes["rework_traceability_lot_codes"]
    assert rework_event.timestamp > primary_event.timestamp

    timestamps = [event.timestamp for event, _ in collected]
    assert len(timestamps) == len(set(timestamps))
    for earlier, later in zip(timestamps, timestamps[1:]):
        assert later > earlier


# ---------------------------------------------------------------------------
# #97 -- rework lots get their own CTE record, so lineage_edges keeps the
# edge into whatever later transformation consumes them
# ---------------------------------------------------------------------------


def test_rework_lot_gets_its_own_transformation_record():
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    _, (primary_event, primary_parents), (rework_event, rework_parents) = _run_past_first_rework(
        engine
    )

    assert rework_event.traceability_lot_code in primary_event.kdes["rework_traceability_lot_codes"]
    assert rework_event.cte_type == CTEType.TRANSFORMATION
    assert rework_event.quantity > 0
    # Same batch/inputs as the transformation that produced it.
    assert rework_parents == primary_parents
    assert rework_event.input_traceability_lot_codes == primary_event.input_traceability_lot_codes
    assert validate_event_like_regengine(rework_event) == []


def test_lineage_edges_keep_the_rework_input_edge_once_it_is_later_consumed(tmp_path):
    """Direct regression test for #97's acceptance criteria: drive the
    engine, storing every event exactly as the running app would, until a
    rework lot both gets its own record and is later resampled as another
    transformation's input. EventStore.lineage_edges must no longer
    silently drop that edge, and the edge count into the consuming
    transformation must equal its declared input-lot count.
    """
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))

    all_records: list[StoredEventRecord] = []
    rework_lot_codes: set[str] = set()
    consuming_event: RegEngineEvent | None = None

    for _ in range(400):
        event, parents = engine.next_event()
        all_records.append(
            StoredEventRecord(payload_source="test", event=event, parent_lot_codes=list(parents))
        )

        if event.cte_type == CTEType.TRANSFORMATION:
            rework_lot_codes.update(event.kdes.get("rework_traceability_lot_codes") or ())
            consumed_here = rework_lot_codes & set(event.input_traceability_lot_codes or ())
            if consumed_here and consuming_event is None:
                consuming_event = event

        if consuming_event is not None:
            break

    assert consuming_event is not None, (
        "expected a rework lot to be minted, recorded, and later consumed "
        "as a transformation input within this run"
    )

    stored = store.add_many(all_records)
    edges = store.lineage_edges(stored)
    nodes = store.lineage_nodes(stored)

    consumed_rework_codes = rework_lot_codes & set(consuming_event.input_traceability_lot_codes or ())
    assert consumed_rework_codes

    node_lot_codes = {node.lot_code for node in nodes}
    edge_pairs = {(edge.source_lot_code, edge.target_lot_code) for edge in edges}

    for rework_lot_code in consumed_rework_codes:
        assert rework_lot_code in node_lot_codes, (
            f"rework lot {rework_lot_code} has no lineage node -- it must have its own record"
        )
        assert (rework_lot_code, consuming_event.traceability_lot_code) in edge_pairs, (
            f"lineage_edges dropped the edge from rework lot {rework_lot_code} "
            f"into {consuming_event.traceability_lot_code}"
        )

    # The stronger #97 acceptance criterion: the edge count into the
    # consuming transformation equals its declared input-lot count exactly
    # -- not just "the rework edge happens to be among them".
    edges_into_consumer = [
        edge for edge in edges if edge.target_lot_code == consuming_event.traceability_lot_code
    ]
    assert len(edges_into_consumer) == len(consuming_event.input_traceability_lot_codes or ())


def test_lineage_tracing_resolves_input_lots_through_a_transformation(tmp_path):
    """'Lineage tracing still resolves input lots': EventStore.lineage()
    must walk from a transformation's output lot back through to every one
    of its declared input lots, via the top-level field #91 adds."""
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))

    all_records: list[StoredEventRecord] = []
    transformation_event: RegEngineEvent | None = None
    for _ in range(200):
        event, parents = engine.next_event()
        all_records.append(
            StoredEventRecord(payload_source="test", event=event, parent_lot_codes=list(parents))
        )
        if transformation_event is None and event.cte_type == CTEType.TRANSFORMATION and len(parents) >= 2:
            transformation_event = event
            break

    assert transformation_event is not None
    store.add_many(all_records)

    traced = store.lineage(transformation_event.traceability_lot_code)
    traced_lot_codes = {record.event.traceability_lot_code for record in traced}

    assert transformation_event.traceability_lot_code in traced_lot_codes
    for input_lot_code in transformation_event.input_traceability_lot_codes:
        assert input_lot_code in traced_lot_codes, (
            f"lineage() failed to resolve input lot {input_lot_code} for "
            f"{transformation_event.traceability_lot_code}"
        )


# ---------------------------------------------------------------------------
# #115 -- one CTE record per transformation OUTPUT lot, not just outputs[0]
#
# _transform() mints 1-3 output lots (_transform_output_count) and appends
# every one to self.transformed, but used to build a single RegEngineEvent
# populated entirely from outputs[0]. The other output lots appeared only
# inside that event's kdes["output_traceability_lot_codes"] array. Nothing in
# lineage traversal reads that array -- EventStore._parent_lot_codes links a
# record to its parents via parent_lot_codes, source_traceability_lot_code
# and input_traceability_lot_codes only -- so those lots had no record of
# their own until they shipped, at which point _ship() stamped the shipment's
# parent_lot_codes with the *pre-transformation* inputs. The lot's own
# history then began with a SHIPPING event pointing straight back past the
# transformation that created it.
# ---------------------------------------------------------------------------


def _drive(engine: LegitFlowEngine, calls: int) -> list[tuple[RegEngineEvent, list[str]]]:
    """Drive the engine, then drain anything still queued.

    Draining matters: the extra output records are queued on
    _pending_events, so a plain N-call loop can stop mid-batch and make a
    complete batch look incomplete.
    """
    collected = [engine.next_event() for _ in range(calls)]
    while engine._pending_events:
        collected.append(engine._pending_events.pop(0))
    return collected


def _batches(collected: list[tuple[RegEngineEvent, list[str]]]) -> dict[str, dict]:
    """Group transformation events by batch_number, recording the outputs the
    batch declared and the output lots that actually got a record.
    """
    batches: dict[str, dict] = {}
    for event, _ in collected:
        if event.cte_type is not CTEType.TRANSFORMATION:
            continue
        batch = batches.setdefault(
            event.kdes["batch_number"],
            {"declared": tuple(event.kdes.get("output_traceability_lot_codes") or ()), "recorded": set()},
        )
        if event.traceability_lot_code in batch["declared"]:
            batch["recorded"].add(event.traceability_lot_code)
    return batches


@pytest.mark.parametrize(
    "scenario",
    ["fresh_cut_processor", "seafood_first_receiver", "leafy_greens_supplier"],
)
def test_every_transformation_output_lot_gets_its_own_record(scenario: str) -> None:
    """Acceptance criterion 1. Before the fix every multi-output batch was
    missing a record for at least one of its own declared output lots.
    """
    engine = LegitFlowEngine(seed=SEED, scenario=scenario)
    batches = _batches(_drive(engine, 600))

    multi_output = {
        number: batch for number, batch in batches.items() if len(batch["declared"]) > 1
    }
    assert multi_output, f"{scenario} produced no multi-output transformation to test"

    for number, batch in batches.items():
        assert batch["recorded"] == set(batch["declared"]), (
            f"{scenario} batch {number}: output lots with no TRANSFORMATION record of "
            f"their own: {sorted(set(batch['declared']) - batch['recorded'])}"
        )


def test_a_later_output_lots_own_record_carries_its_own_quantity_and_description() -> None:
    """The old single event described outputs[0] only, so a sibling lot that
    did get shipped later carried outputs[0]'s quantity and description in
    the only transformation record naming that batch.
    """
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    collected = _drive(engine, 600)

    by_batch: dict[str, list[RegEngineEvent]] = {}
    for event, _ in collected:
        if event.cte_type is CTEType.TRANSFORMATION and event.traceability_lot_code in (
            event.kdes.get("output_traceability_lot_codes") or ()
        ):
            by_batch.setdefault(event.kdes["batch_number"], []).append(event)

    multi = [events for events in by_batch.values() if len(events) > 1]
    assert multi, "expected at least one batch with more than one output record"

    for events in multi:
        lot_codes = [event.traceability_lot_code for event in events]
        assert len(set(lot_codes)) == len(lot_codes), "each output lot gets a distinct record"
        # Distinct identity per record, not a copy of outputs[0].
        assert len({event.product_description for event in events}) == len(events)
        for event in events:
            assert event.quantity > 0
            # Batch-level linkage is shared, which is what makes them one
            # transformation rather than several.
            assert event.input_traceability_lot_codes == events[0].input_traceability_lot_codes
            assert event.kdes["batch_number"] == events[0].kdes["batch_number"]
            # Per-lot source reference, not outputs[0]'s.
            assert event.kdes["tlc_source_reference"] == event.kdes[
                "traceability_lot_code_source_reference"
            ]
        assert len({event.kdes["tlc_source_reference"] for event in events}) == len(events)
        # #119: no two events may share a timestamp.
        assert len({event.timestamp for event in events}) == len(events)
        assert validate_event_like_regengine(events[-1]) == []


def test_lineage_for_a_second_output_lot_includes_its_own_transformation_record(tmp_path) -> None:
    """Acceptance criterion 2. Drive the engine storing every event the way
    the running app does, find an output lot that is NOT outputs[0] of its
    batch and that later ships, and assert store.lineage() for it shows the
    transformation that created it -- not a SHIPPING record linking straight
    back to the pre-transformation inputs.
    """
    engine = LegitFlowEngine(seed=SEED, scenario=TRANSFORM_SCENARIO)
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))

    collected = _drive(engine, 600)
    store.add_many(
        [
            StoredEventRecord(payload_source="test", event=event, parent_lot_codes=list(parents))
            for event, parents in collected
        ]
    )

    # A non-first output lot of some batch that also has a SHIPPING record.
    shipped_lots = {
        event.traceability_lot_code
        for event, _ in collected
        if event.cte_type is CTEType.SHIPPING
    }
    candidates = []
    for event, _ in collected:
        if event.cte_type is not CTEType.TRANSFORMATION:
            continue
        declared = event.kdes.get("output_traceability_lot_codes") or ()
        for later_output in declared[1:]:
            if later_output in shipped_lots:
                candidates.append(later_output)
    assert candidates, "expected a non-first output lot that later ships"

    lot_code = candidates[0]
    lineage = store.lineage(lot_code)
    own_records = [
        record for record in lineage if record.event.traceability_lot_code == lot_code
    ]
    assert own_records, f"{lot_code} has no records at all in its lineage"

    own_cte_types = [record.event.cte_type for record in own_records]
    assert CTEType.TRANSFORMATION in own_cte_types, (
        f"{lot_code}'s own history is {own_cte_types} -- it must include the "
        "TRANSFORMATION that created it, not begin at SHIPPING"
    )
    # And that transformation record comes first: the lot exists before it moves.
    assert own_records[0].event.cte_type is CTEType.TRANSFORMATION

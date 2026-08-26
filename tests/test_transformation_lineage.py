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
) -> list[tuple[RegEngineEvent, list[str]]]:
    """Drive ``engine`` until a transformation reporting a rework lot has
    been observed, then one call further to also capture that rework lot's
    own queued CTE record. The two are always adjacent in that order (see
    app/engine.py::_transform / next_event): the primary event is always
    returned synchronously from the very call that performs the
    transformation, and its rework twin is always the very next
    ``next_event()`` call after that, ahead of any freshly chosen action.
    """
    collected: list[tuple[RegEngineEvent, list[str]]] = []
    for _ in range(attempts):
        collected.append(engine.next_event())
        event, _ = collected[-1]
        if event.cte_type == CTEType.TRANSFORMATION and event.kdes.get("rework_traceability_lot_codes"):
            collected.append(engine.next_event())
            return collected
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
    collected = _run_past_first_rework(engine)

    primary_event, _ = collected[-2]
    rework_event, _ = collected[-1]

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
    collected = _run_past_first_rework(engine)
    primary_event, primary_parents = collected[-2]
    rework_event, rework_parents = collected[-1]

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

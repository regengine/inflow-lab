"""Regression coverage for issues #157 and #159.

Mirrors tests/test_conformance.py's approach for #186-#189: render each
export through its real, unmocked render_* function and assert on the
actual output. Where DEMO_FIXTURES already exercises the scenario (the
demo transformation's genuine absence of per-input quantity data), that
bundled fixture data is used directly. DEMO_FIXTURES' own dt() helper
always normalizes timestamps to UTC before construction, though, so it
never produces a non-UTC-offset timestamp or a transformation event with a
populated input_lot_quantities KDE -- those two specific edge cases are
exercised with hand-built RegEngineEvent/StoredEventRecord objects instead,
still run through the real render functions.
"""

from __future__ import annotations

import json
import csv
import io
from datetime import UTC, datetime, timedelta, timezone

from app.demo_fixtures import DEMO_FIXTURES
from app.epcis_export import render_epcis_document
from app.fda_export import normalize_to_utc, render_fda_request_csv
from app.schemas.domain import CTEType, DemoFixtureId, RegEngineEvent, StoredEventRecord


def _records_for(fixture_id: DemoFixtureId) -> list[StoredEventRecord]:
    fixture = DEMO_FIXTURES[fixture_id]
    return [
        StoredEventRecord(
            sequence_no=index,
            payload_source="test-export-correctness",
            event=fixture_event.event,
            parent_lot_codes=list(fixture_event.parent_lot_codes),
        )
        for index, fixture_event in enumerate(fixture.events, start=1)
    ]


def _record(event: RegEngineEvent, **kwargs: object) -> StoredEventRecord:
    return StoredEventRecord(payload_source="test-export-correctness", event=event, **kwargs)


def _no_gln(_location_name: str) -> str:
    return ""


def _fda_rows(records: list[StoredEventRecord]) -> list[dict[str, str]]:
    csv_text = render_fda_request_csv(records, location_gln=_no_gln)
    return list(csv.DictReader(io.StringIO(csv_text)))


def _epcis_events(records: list[StoredEventRecord]) -> list[dict]:
    document = render_epcis_document(
        records,
        source="test-export-correctness",
        location_gln=_no_gln,
        creation_date=datetime(2026, 2, 5, tzinfo=UTC),
    )
    return document["epcisBody"]["eventList"]


# ---------------------------------------------------------------------------
# #157 -- FDA export splits Date/Time off of a raw, non-normalized timestamp
# ---------------------------------------------------------------------------


def _event_at(timestamp: datetime, *, lot_code: str = "TLC-TS-1") -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.RECEIVING,
        traceability_lot_code=lot_code,
        product_description="Romaine Lettuce",
        quantity=10.0,
        unit_of_measure="cases",
        location_name="Distribution Center #4",
        timestamp=timestamp,
        kdes={},
    )


def test_fda_export_normalizes_a_positive_offset_timestamp_to_utc() -> None:
    """Reproduces issue #157's own repro case: a timestamp carrying an
    explicit non-UTC offset must be converted to UTC before Date/Time are
    split off of it, not exported using the offset's own local reading."""
    event = _event_at(datetime(2026, 2, 5, 23, 30, 0, tzinfo=timezone(timedelta(hours=5))))
    row = _fda_rows([_record(event)])[0]

    # 2026-02-05T23:30:00+05:00 is 2026-02-05T18:30:00Z.
    assert row["Date"] == "2026-02-05"
    assert row["Time"] == "18:30:00"


def test_fda_export_two_events_five_hours_apart_never_collide_on_date_and_time() -> None:
    """The issue's headline impact case: a +05:00 event and a Z event that
    share the same local wall-clock digits must not render identical
    Date/Time once they are actually five hours apart in absolute time."""
    offset_event = _event_at(
        datetime(2026, 2, 5, 23, 30, 0, tzinfo=timezone(timedelta(hours=5))),
        lot_code="TLC-TS-OFFSET",
    )
    utc_event = _event_at(datetime(2026, 2, 5, 23, 30, 0, tzinfo=UTC), lot_code="TLC-TS-UTC")
    rows = _fda_rows([_record(offset_event), _record(utc_event)])
    by_lot = {row["Traceability Lot Code"]: row for row in rows}

    assert (by_lot["TLC-TS-OFFSET"]["Date"], by_lot["TLC-TS-OFFSET"]["Time"]) == ("2026-02-05", "18:30:00")
    assert (by_lot["TLC-TS-UTC"]["Date"], by_lot["TLC-TS-UTC"]["Time"]) == ("2026-02-05", "23:30:00")
    assert by_lot["TLC-TS-OFFSET"] != by_lot["TLC-TS-UTC"]


def test_fda_export_normalizes_a_negative_offset_timestamp_across_a_date_boundary() -> None:
    """A negative offset can push the UTC date to the *next* calendar day --
    this only comes out right if normalization happens before .date() is
    ever read, not after."""
    event = _event_at(datetime(2026, 2, 5, 22, 0, 0, tzinfo=timezone(timedelta(hours=-5))))
    row = _fda_rows([_record(event)])[0]

    # 2026-02-05T22:00:00-05:00 is 2026-02-06T03:00:00Z.
    assert row["Date"] == "2026-02-06"
    assert row["Time"] == "03:00:00"


def test_fda_export_treats_a_naive_timestamp_as_already_utc_not_local_time() -> None:
    """A tzinfo-less timestamp is read as UTC -- matching
    csv_importer._ensure_timezone's own convention for naive rows -- rather
    than handed to .astimezone(), which would reinterpret it using
    whatever local timezone the export happens to run under and make the
    output depend on where the process is deployed."""
    event = _event_at(datetime(2026, 2, 5, 23, 30, 0))  # no tzinfo
    row = _fda_rows([_record(event)])[0]

    assert row["Date"] == "2026-02-05"
    assert row["Time"] == "23:30:00"


def test_fda_export_demo_fixture_dates_are_unaffected_by_normalization() -> None:
    """Every bundled demo fixture is already authored in UTC (via
    demo_fixtures.dt()'s own .astimezone(UTC)), so normalizing must be a
    no-op for the export's existing, already-correct real-fixture output --
    this fix must not shift dates that were never wrong."""
    for fixture_id in DemoFixtureId:
        records = _records_for(fixture_id)
        rows = _fda_rows(records)
        for row, record in zip(rows, records):
            assert row["Date"] == record.event.timestamp.date().isoformat()
            assert row["Time"] == record.event.timestamp.time().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# #159 -- EPCIS TransformationEvent inputQuantityList entries carry no
# quantity
# ---------------------------------------------------------------------------


def test_engine_generated_transformation_has_no_per_input_quantity_data_today() -> None:
    """Documents the actual, current state of the data rather than assuming
    it: the bundled fresh_cut_transformation demo fixture -- the same shape
    of data the engine itself generates -- has no per-input quantity
    anywhere in its TransformationEvent's kdes, so inputQuantityList
    entries correctly have no quantity/uom key. This is issue #159's own
    documented finding (independently confirmed by
    app/cte_rules.py's TRANSFORMATION_INPUT_LINKAGE_KDES comment and by
    app/industry_adapters.py's transformation_kdes, which only ever
    computes an aggregate yield_ratio, never a per-lot figure) -- not a gap
    this test suite is failing to catch. Lot identity is still populated;
    only the quantity that nothing upstream captures is absent.
    """
    records = _records_for(DemoFixtureId.FRESH_CUT_TRANSFORMATION)
    events = _epcis_events(records)
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")

    assert transformation_event["inputQuantityList"], "sanity: fixture has input lots"
    for entry in transformation_event["inputQuantityList"]:
        assert "quantity" not in entry
        assert "uom" not in entry
        assert entry["regengine:traceabilityLotCode"]
        assert entry["epcClass"]


def _transformation_record(input_lot_quantities: dict[str, dict[str, object]] | None) -> StoredEventRecord:
    kdes: dict[str, object] = {
        "transformation_date": "2026-02-06",
        "transformation_location": "ReadyFresh Processing Plant",
        "input_traceability_lot_codes": ["TLC-IN-1", "TLC-IN-2"],
        "input_products": ["Romaine Lettuce", "Spinach"],
        "reference_document_type": "Batch Record",
        "reference_document_number": "BATCH-TEST-001",
    }
    if input_lot_quantities is not None:
        kdes["input_lot_quantities"] = input_lot_quantities
    event = RegEngineEvent(
        cte_type=CTEType.TRANSFORMATION,
        traceability_lot_code="TLC-OUT-1",
        product_description="Fresh Cut Salad Mix",
        quantity=100.0,
        unit_of_measure="cases",
        location_name="ReadyFresh Processing Plant",
        timestamp=datetime(2026, 2, 6, 17, 20, 0, tzinfo=UTC),
        kdes=kdes,
    )
    return _record(event, parent_lot_codes=["TLC-IN-1", "TLC-IN-2"])


def test_input_quantity_is_populated_when_the_data_is_actually_present() -> None:
    """When a per-input quantity/uom *is* available -- e.g. a hand-crafted
    or CSV-imported event whose free-form kdes JSON supplies an
    input_lot_quantities mapping -- the exporter must use it, keyed by lot
    code so lookup is correct regardless of which of _input_lot_codes'
    three merged sources produced a given lot code."""
    record = _transformation_record(
        {
            "TLC-IN-1": {"quantity": 288, "unit_of_measure": "cases"},
            "TLC-IN-2": {"quantity": 248, "unit_of_measure": "cases"},
        }
    )
    events = _epcis_events([record])
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")
    by_lot = {entry["regengine:traceabilityLotCode"]: entry for entry in transformation_event["inputQuantityList"]}

    assert by_lot["TLC-IN-1"]["quantity"] == 288
    assert by_lot["TLC-IN-1"]["uom"] == "cases"
    assert by_lot["TLC-IN-2"]["quantity"] == 248
    assert by_lot["TLC-IN-2"]["uom"] == "cases"


def test_input_quantity_partial_coverage_only_fills_in_the_lot_it_covers() -> None:
    """A partial input_lot_quantities mapping must not fabricate a value
    for the lot code it doesn't cover -- half-known is not license to
    guess the other half."""
    record = _transformation_record({"TLC-IN-1": {"quantity": 288, "unit_of_measure": "cases"}})
    events = _epcis_events([record])
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")
    by_lot = {entry["regengine:traceabilityLotCode"]: entry for entry in transformation_event["inputQuantityList"]}

    assert by_lot["TLC-IN-1"]["quantity"] == 288
    assert "quantity" not in by_lot["TLC-IN-2"]


def test_input_quantity_ignores_a_boolean_masquerading_as_numeric() -> None:
    """bool is a subclass of int in Python -- a stray True/False surviving
    from hand-authored or CSV-sourced kdes must not silently become a fake
    quantity of 1 or 0."""
    record = _transformation_record({"TLC-IN-1": {"quantity": True, "unit_of_measure": "cases"}})
    events = _epcis_events([record])
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")
    entry = next(
        e for e in transformation_event["inputQuantityList"] if e["regengine:traceabilityLotCode"] == "TLC-IN-1"
    )

    assert "quantity" not in entry


def test_input_quantity_absent_entirely_matches_the_pre_fix_shape() -> None:
    """No input_lot_quantities KDE at all -- today's actual upstream
    reality for every engine-generated transformation -- must still render
    a valid QuantityElement: lot identity present, quantity/uom simply
    absent, not a raised error or a null placeholder."""
    record = _transformation_record(None)
    events = _epcis_events([record])
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")

    for entry in transformation_event["inputQuantityList"]:
        assert "quantity" not in entry
        assert "uom" not in entry
        assert entry["epcClass"]
        assert entry["regengine:traceabilityLotCode"]


def test_output_quantity_list_is_unaffected_by_the_input_quantity_change() -> None:
    """outputQuantityList already worked correctly before this fix (it has
    always passed event.quantity/unit_of_measure) -- guard against a
    regression there while touching the surrounding function."""
    record = _transformation_record(None)
    events = _epcis_events([record])
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")

    assert transformation_event["outputQuantityList"] == [
        {
            "epcClass": "urn:regengine:lot:TLC-OUT-1",
            "regengine:traceabilityLotCode": "TLC-OUT-1",
            "quantity": 100.0,
            "uom": "cases",
            "regengine:productDescription": "Fresh Cut Salad Mix",
        }
    ]


# ---------------------------------------------------------------------------
# #159 — the engine-generated path must actually populate input_lot_quantities
# ---------------------------------------------------------------------------


def test_engine_generated_transformation_carries_per_input_quantities():
    """The gap #159 was actually about.

    The exporter has always read ``kdes["input_lot_quantities"]``, but nothing
    in ``app/`` ever wrote it -- a repo-wide search found the key only in
    ``epcis_export.py`` and its own tests -- so every engine-generated and
    demo-fixture transformation rendered ``inputQuantityList`` entries bare.
    The tests around it built their records by hand and so could not see that.

    The data was never missing: ``industry_adapters.transformation_kdes``
    receives ``inputs: list[Lot]``, each carrying ``.quantity`` and
    ``.unit_of_measure``, and already derived ``input_traceability_lot_codes``
    from that same list.

    This drives the real engine, so it fails if the adapter stops populating
    the key regardless of what the hand-built fixtures assert.
    """
    from app.engine import LegitFlowEngine

    engine = LegitFlowEngine(seed=204)
    for _ in range(180):
        event, _ = engine.next_event()
        if event.cte_type != CTEType.TRANSFORMATION:
            continue

        per_lot = event.kdes.get("input_lot_quantities")
        assert isinstance(per_lot, dict) and per_lot, (
            "engine-generated transformation carries no input_lot_quantities; "
            "EPCIS inputQuantityList would render every entry bare"
        )

        # Every declared input lot must have a usable quantity, and the keys
        # must line up with the lot codes the same adapter emits -- a mapping
        # keyed on something else would silently miss on export.
        for lot_code in event.kdes["input_traceability_lot_codes"]:
            entry = per_lot.get(lot_code)
            assert isinstance(entry, dict), f"no quantity entry for input lot {lot_code}"
            assert isinstance(entry["quantity"], (int, float))
            assert not isinstance(entry["quantity"], bool)
            assert entry["quantity"] > 0
            assert isinstance(entry["unit_of_measure"], str)
            assert entry["unit_of_measure"]
        return

    raise AssertionError("Expected a transformation event within 180 events")


# ---------------------------------------------------------------------------
# #157 — the day filter and the exported Date column must agree
# ---------------------------------------------------------------------------


def test_fda_day_filter_agrees_with_the_exported_date_column(tmp_path):
    """#157 normalized the exported Date/Time columns to UTC but left the
    day filter keyed on the raw local date, desyncing the two.

    ``event.timestamp`` keeps whatever offset the source carried -- a CSV
    row's explicit ``+05:00`` survives import untouched -- so an event at
    ``2026-02-05T02:00:00+05:00`` exports a Date column of ``2026-02-04``
    while ``all_between`` filed it under ``2026-02-05``. A day-scoped FDA
    request could then omit exactly the rows printing that day, and return
    rows printing a different one. Before #157 both sides read the raw local
    date and agreed; normalizing only the column is what split them.
    """
    from app.store import EventStore

    offset_timestamp = datetime(2026, 2, 5, 2, 0, tzinfo=timezone(timedelta(hours=5)))
    record = _transformation_record(None)
    record.event.timestamp = offset_timestamp

    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.add_many([record])

    exported_day = normalize_to_utc(offset_timestamp).date().isoformat()
    assert exported_day == "2026-02-04", "fixture no longer exercises an offset that shifts the day"

    # The day the export prints must be the day the filter returns it for.
    same_day = store.all_between(start_date=exported_day, end_date=exported_day)
    assert len(same_day) == 1, (
        f"a row whose exported Date column reads {exported_day} was not returned "
        f"by a request scoped to {exported_day}"
    )

    # And it must not also answer to the raw local date, which is the
    # symptom of the two sides keying on different things.
    local_day = offset_timestamp.date().isoformat()
    assert local_day == "2026-02-05"
    assert store.all_between(start_date=local_day, end_date=local_day) == []


# ---------------------------------------------------------------------------
# #162 — an imported location_gln must reach both exports, not just the model
# ---------------------------------------------------------------------------


def test_imported_location_gln_reaches_both_exports():
    """#162 was only half done.

    ``csv_importer`` correctly threads a GLN column into
    ``RegEngineEvent.location_gln``, and a test asserted that parsed field. But
    both exporters keyed solely on the engine's static name->GLN registry --
    which an imported location is not in -- so the FDA "Location Identifier
    (GLN)" cell came out empty and the GLN never appeared in the EPCIS
    document at all. The half that was dropped is the half a regulator sees.
    """
    gln = "0812345000013"
    record = _transformation_record(None)
    record.event.location_name = "Nowhere Farm"
    record.event.location_gln = gln

    def _registry_miss(_location_name: str) -> str:
        # The engine's registry does not know an imported location, which is
        # exactly the condition under test.
        return ""

    csv_text = render_fda_request_csv([record], location_gln=_registry_miss)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows, "no rows exported"
    assert rows[0]["Location Identifier (GLN)"] == gln, (
        "the imported GLN did not reach the FDA export column"
    )

    document = render_epcis_document(
        [record],
        source="test",
        location_gln=_registry_miss,
        creation_date=datetime(2026, 2, 5, tzinfo=UTC),
    )
    assert gln in json.dumps(document), "the imported GLN never appeared in the EPCIS document"

    # The registry still wins when it does know the location -- the event's
    # own value is a fallback, not an override.
    known_gln = "0899999000017"
    csv_text = render_fda_request_csv([record], location_gln=lambda _name: known_gln)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["Location Identifier (GLN)"] == known_gln


# ---------------------------------------------------------------------------
# The stack must not contradict its own contract.md
# ---------------------------------------------------------------------------


def test_top_level_input_lot_codes_satisfy_validation_and_reach_epcis():
    """`contract.md` declares the TOP-LEVEL `input_traceability_lot_codes`
    authoritative and says the simulator now emits it there. Only the engine
    path does -- demo fixtures and CSV imports leave it `None`.

    An integrator following the contract, sending the field exactly where the
    contract says to send it and nowhere else, was flagged at required
    severity (promoted by #189 in this same stack) for a KDE they had in fact
    supplied, and their EPCIS `inputQuantityList` came out empty -- silently
    losing the input-to-output lineage link that is the whole point of a
    transformation CTE.
    """
    from app.cte_rules import validate_event_kdes

    record = _transformation_record(None)
    # Exactly what a contract-following integrator sends: top-level only.
    record.event.kdes.pop("input_traceability_lot_codes", None)
    record.event.input_traceability_lot_codes = ["TLC-INPUT-000001", "TLC-INPUT-000002"]

    warnings = validate_event_kdes(record.event)
    missing = [w for w in warnings if w.field == "input_traceability_lot_codes"]
    assert not missing, (
        "a contract-compliant top-level value was still reported missing: "
        f"{[w.message for w in missing]}"
    )

    document = render_epcis_document(
        [record],
        source="test",
        location_gln=lambda _name: "",
        creation_date=datetime(2026, 2, 5, tzinfo=UTC),
    )
    rendered = json.dumps(document)
    for lot_code in record.event.input_traceability_lot_codes:
        assert lot_code in rendered, f"{lot_code} never reached the EPCIS document"


def test_kdes_copy_still_wins_when_both_are_present():
    """Additive, not a replacement -- matching how the top-level field was
    introduced. The local validator, audit checks and exports all read the
    kdes copy, so it must keep taking precedence when both carry a value."""
    from app.cte_rules import merged_event_values

    record = _transformation_record(None)
    record.event.kdes["input_traceability_lot_codes"] = ["TLC-FROM-KDES"]
    record.event.input_traceability_lot_codes = ["TLC-FROM-TOP-LEVEL"]

    assert merged_event_values(record.event)["input_traceability_lot_codes"] == ["TLC-FROM-KDES"]

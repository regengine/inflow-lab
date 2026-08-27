"""Regression coverage for issues #186, #187, #188, and #189.

Each export is rendered from a real ``DEMO_FIXTURES`` entry -- the same
fixtures the running app loads for demos -- rather than a hand-rolled event,
so these tests fail the same way a user clicking "export" would notice the
bug, matching how each issue's evidence was originally reproduced.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from app.cte_rules import REQUIRED_KDES, RECOMMENDED_KDES, validate_event_kdes
from app.demo_fixtures import DEMO_FIXTURES
from app.epcis_export import _BIZ_STEPS, render_epcis_document
from app.fda_export import render_fda_request_csv
from app.schemas.domain import CTEType, DemoFixtureId, RegEngineEvent, StoredEventRecord


def _records_for(fixture_id: DemoFixtureId) -> list[StoredEventRecord]:
    fixture = DEMO_FIXTURES[fixture_id]
    return [
        StoredEventRecord(
            sequence_no=index,
            payload_source="test-conformance",
            event=fixture_event.event,
            parent_lot_codes=list(fixture_event.parent_lot_codes),
        )
        for index, fixture_event in enumerate(fixture.events, start=1)
    ]


def _no_gln(_location_name: str) -> str:
    return ""


# ---------------------------------------------------------------------------
# #186 -- FDA CSV export omits required Shipping/Receiving location and
# TLC-source KDEs
# ---------------------------------------------------------------------------


def _fda_rows(records: list[StoredEventRecord]) -> list[dict[str, str]]:
    csv_text = render_fda_request_csv(records, location_gln=_no_gln)
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_fda_export_shipping_only_row_includes_ship_to_and_tlc_source() -> None:
    """A Shipping row exported alone must still carry its ship-to KDE.

    Reproduces issue #186's own repro case: exporting Shipping without its
    paired Receiving leg previously dropped the recipient location entirely.
    """
    records = _records_for(DemoFixtureId.LEAFY_GREENS_TRACE)
    shipping_only = [r for r in records if r.event.cte_type == CTEType.SHIPPING]
    assert len(shipping_only) == 1  # sanity: genuinely a single, unpaired row

    rows = _fda_rows(shipping_only)
    assert len(rows) == 1
    row = rows[0]
    assert row["Location Description"] == "FreshPack Central"
    assert row["Ship-To / Previous Source Location Description"] == "Distribution Center #4"
    assert row["TLC Source Reference"] == "SRC-DEMO-LG-PACK-001"


def test_fda_export_receiving_only_row_includes_previous_source_and_tlc_source() -> None:
    """A Receiving row exported alone must still carry its previous-source KDE."""
    records = _records_for(DemoFixtureId.LEAFY_GREENS_TRACE)
    receiving_only = [r for r in records if r.event.cte_type == CTEType.RECEIVING]
    assert len(receiving_only) == 1

    rows = _fda_rows(receiving_only)
    assert len(rows) == 1
    row = rows[0]
    assert row["Location Description"] == "Distribution Center #4"
    assert row["Ship-To / Previous Source Location Description"] == "FreshPack Central"
    assert row["TLC Source Reference"] == "SRC-DEMO-LG-PACK-001"


def test_tlc_source_reference_exported_for_every_cte_type_that_requires_it() -> None:
    """Acceptance criterion: tlc_source_reference must be present in the
    export for every CTE type app/cte_rules.py lists it as required for
    (today: Shipping and Receiving) -- checked dynamically against
    REQUIRED_KDES rather than hardcoded, so this stays true if that set
    ever changes.
    """
    all_records = [record for fixture_id in DemoFixtureId for record in _records_for(fixture_id)]
    required_cte_types = {
        cte_type for cte_type, fields in REQUIRED_KDES.items() if "tlc_source_reference" in fields
    }
    assert required_cte_types, "expected at least one CTE type to require tlc_source_reference"

    for cte_type in required_cte_types:
        matching = [r for r in all_records if r.event.cte_type == cte_type]
        assert matching, f"demo fixtures should exercise {cte_type} for this check to mean anything"
        for row in _fda_rows(matching):
            assert row["TLC Source Reference"], f"{cte_type.value} row missing TLC Source Reference: {row}"


# ---------------------------------------------------------------------------
# #187 -- EPCIS export never uses sourceList/destinationList for
# ship-to/previous-source
# ---------------------------------------------------------------------------


def _epcis_events(fixture_id: DemoFixtureId) -> list[dict]:
    records = _records_for(fixture_id)
    document = render_epcis_document(
        records,
        source="test-conformance",
        location_gln=_no_gln,
        creation_date=datetime(2026, 2, 5, tzinfo=UTC),
    )
    return document["epcisBody"]["eventList"]


def test_shipping_event_carries_destination_list_for_ship_to_location() -> None:
    events = _epcis_events(DemoFixtureId.LEAFY_GREENS_TRACE)
    shipping_event = next(e for e in events if e["regengine:cteType"] == "shipping")

    assert shipping_event["destinationList"] == [
        {
            "type": "location",
            "destination": "urn:regengine:location:Distribution%20Center%20%234",
        }
    ]
    assert "sourceList" not in shipping_event
    # Additive, not a replacement -- the vendor extension still carries it too.
    assert shipping_event["regengine:kdes"]["ship_to_location"] == "Distribution Center #4"


def test_receiving_event_carries_source_list_for_previous_source() -> None:
    events = _epcis_events(DemoFixtureId.LEAFY_GREENS_TRACE)
    receiving_event = next(e for e in events if e["regengine:cteType"] == "receiving")

    assert receiving_event["sourceList"] == [
        {
            "type": "location",
            "source": "urn:regengine:location:FreshPack%20Central",
        }
    ]
    assert "destinationList" not in receiving_event
    assert receiving_event["regengine:kdes"]["immediate_previous_source"] == "FreshPack Central"


def test_source_and_destination_types_use_gs1s_declared_vocabulary_tokens() -> None:
    """The `type` member must be a bare CBV vocabulary token, never a URN.

    GS1's own epcis-context.jsonld declares sourceList/destinationList `type`
    as ``"@type": "@vocab"`` over exactly three short names, so only those
    expand to the intended cbv:SDT-* term. The official EPCIS 2.0 JSON Schema
    additionally rejects the ``urn:epcglobal:cbv`` prefix here outright, via a
    negative lookahead on source-dest-type -- emitting the (otherwise
    legitimate) ``urn:epcglobal:cbv:sdt:location`` alias made every shipping
    and receiving document schema-invalid.

    Pinned as a set membership rather than a literal so this keeps failing for
    any URN, not just the one that was there.
    """
    declared_tokens = {"owning_party", "possessing_party", "location"}

    events = _epcis_events(DemoFixtureId.LEAFY_GREENS_TRACE)
    seen = 0
    for event in events:
        for entry in event.get("sourceList", []) + event.get("destinationList", []):
            seen += 1
            assert entry["type"] in declared_tokens, (
                f"{entry['type']!r} is not one of GS1's declared sourceDestinationType "
                "tokens; a URN here fails both JSON-LD expansion and the EPCIS 2.0 schema"
            )
    assert seen, "fixture produced no source/destination entries to check"


def test_non_handoff_events_carry_neither_source_nor_destination_list() -> None:
    """Harvesting/Cooling/Packing/Transformation have no ship-to or
    previous-source KDE, so sourceList/destinationList must not appear."""
    events = _epcis_events(DemoFixtureId.FRESH_CUT_TRANSFORMATION)
    for event in events:
        if event["regengine:cteType"] in ("shipping", "receiving"):
            continue
        assert "sourceList" not in event
        assert "destinationList" not in event


# ---------------------------------------------------------------------------
# #188 -- Transformation events emit a non-standard bizStep URI
# ---------------------------------------------------------------------------

# GS1's own published EPCIS JSON-LD context (epcis-context.jsonld) defines
# exactly these 41 bizStep terms -- reproduced here per issue #188's own
# evidence rather than fetched at test time, since these tests must run
# offline and the vocabulary is a fixed, versioned standard.
_STANDARD_CBV_BIZ_STEPS = {
    "accepting", "arriving", "assembling", "collecting", "commissioning",
    "consigning", "creating_class_instance", "cycle_counting", "decommissioning",
    "departing", "destroying", "disassembling", "dispensing", "encoding",
    "entering_exiting", "holding", "inspecting", "installing", "killing",
    "loading", "other", "packing", "picking", "receiving", "removing",
    "repackaging", "repairing", "replacing", "reserving", "retail_selling",
    "sampling", "sensor_reporting", "shipping", "staging_outbound",
    "stock_taking", "stocking", "storing", "transporting", "unloading",
    "unpacking", "void_shipping",
}
_CBV_BIZSTEP_PREFIX = "urn:epcglobal:cbv:bizstep:"


def test_every_cbv_namespaced_bizstep_is_a_standard_cbv_term() -> None:
    """Acceptance criterion: every _BIZ_STEPS value using the reserved CBV
    prefix must be one of GS1's real, standard terms -- not just the
    Transformation one this issue was filed about."""
    for cte_type, uri in _BIZ_STEPS.items():
        if not uri.startswith(_CBV_BIZSTEP_PREFIX):
            continue
        term = uri.removeprefix(_CBV_BIZSTEP_PREFIX)
        assert term in _STANDARD_CBV_BIZ_STEPS, f"{cte_type} maps to non-standard CBV term {term!r}"


def test_transformation_bizstep_no_longer_masquerades_as_standard_cbv() -> None:
    uri = _BIZ_STEPS[CTEType.TRANSFORMATION]
    assert uri != "urn:epcglobal:cbv:bizstep:transforming"
    # Either a genuine standard term, or minted entirely outside the
    # reserved CBV namespace -- both satisfy the issue's acceptance criteria.
    if uri.startswith(_CBV_BIZSTEP_PREFIX):
        assert uri.removeprefix(_CBV_BIZSTEP_PREFIX) in _STANDARD_CBV_BIZ_STEPS
    else:
        assert not uri.startswith("urn:epcglobal:cbv:")


def test_transformation_event_renders_the_fixed_bizstep_in_a_real_document() -> None:
    events = _epcis_events(DemoFixtureId.FRESH_CUT_TRANSFORMATION)
    transformation_event = next(e for e in events if e["type"] == "TransformationEvent")
    assert transformation_event["bizStep"] == _BIZ_STEPS[CTEType.TRANSFORMATION]
    assert not transformation_event["bizStep"].startswith(_CBV_BIZSTEP_PREFIX)


# ---------------------------------------------------------------------------
# #189 -- Reclassify Transformation input-lot KDEs from recommended to
# required
# ---------------------------------------------------------------------------


def _transformation_event(**extra_kdes: object) -> RegEngineEvent:
    kdes: dict[str, object] = {field: "placeholder" for field in REQUIRED_KDES[CTEType.TRANSFORMATION]}
    kdes.update(extra_kdes)
    return RegEngineEvent(
        cte_type=CTEType.TRANSFORMATION,
        traceability_lot_code="TLC-CONFORMANCE-OUT",
        product_description="Fresh Cut Salad Mix",
        quantity=100.0,
        unit_of_measure="cases",
        location_name="Processing Plant",
        timestamp=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
        kdes=kdes,
    )


def test_transformation_missing_input_linkage_is_flagged_as_required_not_recommended() -> None:
    """Core acceptance criterion for #189: a transformation event missing
    input lot linkage must be flagged at required (not recommended)
    severity."""
    event = _transformation_event()  # no input_traceability_lot_codes / input_products
    warnings = {w.field: w.message for w in validate_event_kdes(event)}

    for field in ("input_traceability_lot_codes", "input_products"):
        assert field in warnings, f"expected a warning for missing {field}"
        assert warnings[field].startswith("Missing expected"), (
            f"{field} should be flagged at required severity, got: {warnings[field]!r}"
        )
        assert not warnings[field].startswith("Missing recommended")


def test_transformation_with_input_linkage_present_has_no_warning_for_it() -> None:
    event = _transformation_event(
        input_traceability_lot_codes=["TLC-IN-1", "TLC-IN-2"],
        input_products=["Romaine Lettuce", "Spinach"],
    )
    warnings = {w.field for w in validate_event_kdes(event)}
    assert "input_traceability_lot_codes" not in warnings
    assert "input_products" not in warnings


def test_transformation_input_linkage_kdes_are_not_also_listed_as_recommended() -> None:
    """Promoted fields must not double-fire a second, lower-severity
    "recommended" warning alongside the required one."""
    assert "input_traceability_lot_codes" not in RECOMMENDED_KDES[CTEType.TRANSFORMATION]
    assert "input_products" not in RECOMMENDED_KDES[CTEType.TRANSFORMATION]


def test_engine_generated_transformation_fixture_already_satisfies_the_stricter_rule() -> None:
    """Guards the #189 sequencing risk called out for this change: input
    lot linkage is already populated for every transformation the engine
    emits, so tightening validation must not flag the simulator's own
    bundled demo data."""
    fixture = DEMO_FIXTURES[DemoFixtureId.FRESH_CUT_TRANSFORMATION]
    transformation_events = [
        fe.event for fe in fixture.events if fe.event.cte_type == CTEType.TRANSFORMATION
    ]
    assert transformation_events, "fixture should exercise at least one transformation event"

    for event in transformation_events:
        warnings = {w.field for w in validate_event_kdes(event)}
        assert "input_traceability_lot_codes" not in warnings
        assert "input_products" not in warnings


def test_transformation_required_kdes_still_matches_the_regengine_pin() -> None:
    """Documents a deliberate boundary: REQUIRED_KDES itself is pinned
    byte-for-byte to RegEngine's live contract by
    tests/test_regengine_contract_pin.py, so input lot linkage is enforced
    at required severity via a dedicated check in validate_event_kdes
    instead of by editing REQUIRED_KDES -- promoting RegEngine's actual
    contract is a coordinated two-repo change outside this repo's scope.
    If this assertion ever needs to flip, the contract pin needs the
    matching RegEngine-side update first.
    """
    assert "input_traceability_lot_codes" not in REQUIRED_KDES[CTEType.TRANSFORMATION]
    assert "input_products" not in REQUIRED_KDES[CTEType.TRANSFORMATION]


# ---------------------------------------------------------------------------
# #189 -- severity is a real field, and consumers act on it.
# ---------------------------------------------------------------------------


def test_warning_severity_is_a_field_not_a_message_prefix() -> None:
    """#189's promotion of input-lot linkage to required tier was, until
    now, only a change to message text: CTEValidationWarning had no
    severity field and no consumer in app/ told "Missing expected" from
    "Missing recommended". Anything downstream that wanted to act on the
    distinction had to string-match presentation copy. It is data now.
    """
    event = _transformation_event()  # no input linkage, no reference_document_type
    by_field = {warning.field: warning for warning in validate_event_kdes(event)}

    for field in ("input_traceability_lot_codes", "input_products"):
        assert by_field[field].severity == "required", (
            f"{field} is FDA-mandatory input linkage and must carry required severity"
        )
    # A genuinely advisory KDE on the same event must NOT be promoted along
    # with them -- if everything is required, nothing is.
    assert by_field["reference_document_type"].severity == "recommended"


def test_a_malformed_input_lot_list_is_required_severity_too() -> None:
    # Supplying the linkage in the wrong shape satisfies FDA's requirement
    # no better than omitting it, so it cannot be the softer tier.
    event = _transformation_event(input_traceability_lot_codes="TLC-IN-1", input_products=["Romaine"])
    warnings = [w for w in validate_event_kdes(event) if w.field == "input_traceability_lot_codes"]

    assert warnings, "a non-list input lot value should be flagged"
    assert all(warning.severity == "required" for warning in warnings)


def test_csv_import_warnings_carry_severity_to_the_caller() -> None:
    """The CSV importer is one of the two consumers #189 names. Its
    response used to hand back required-tier gaps and advisory nudges in
    one undifferentiated list."""
    from app.csv_importer import parse_csv_import
    from app.schemas.domain import CSVImportType

    csv_text = (
        "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,timestamp\n"
        "transformation,TLC-SEV-000001,Chopped Romaine,50,cases,Fresh Cut Plant,"
        "2026-03-01T08:00:00Z\n"
    )
    result = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    severities = {warning.field: warning.severity for warning in result.warnings}
    assert severities.get("input_traceability_lot_codes") == "required"
    assert severities.get("reference_document_type") == "recommended"


def test_audit_summary_counts_the_two_severities_separately() -> None:
    """The other consumer: the console's audit-readiness summary. Every
    warning counted the same, so a missing FDA-mandatory KDE scored
    identically to a missing nicety."""
    from app.audit import summarize_scenario_audit
    from app.scenarios import SCENARIO_PRESETS, ScenarioId

    scenario = SCENARIO_PRESETS[ScenarioId.FRESH_CUT_PROCESSOR]
    record = StoredEventRecord(
        sequence_no=1,
        payload_source="test-conformance",
        event=_transformation_event(),
    )

    summary = summarize_scenario_audit([record], scenario)

    assert summary["required_warning_count"] > 0
    assert summary["recommended_warning_count"] > 0
    assert (
        summary["required_warning_count"] + summary["recommended_warning_count"]
        == summary["warning_count"]
    )
    payload = summary["warnings_by_record"][record.record_id]
    assert {"required", "recommended"} >= {warning["severity"] for warning in payload}
    assert any(warning["severity"] == "required" for warning in payload)


def test_a_required_gap_is_never_shadowed_by_a_recommended_one_for_the_same_field() -> None:
    """dedupe_warnings has to honour severity, not just uniqueness. The
    shift log renders exactly one warning per row, so if an advisory entry
    for a field could sort ahead of that field's required entry, the
    operator would be shown the softer of the two."""
    from app.cte_rules import CTEValidationWarning, dedupe_warnings

    deduped = dedupe_warnings(
        [
            CTEValidationWarning(field="input_products", message="soft nudge", severity="recommended"),
            CTEValidationWarning(field="input_products", message="hard gap", severity="required"),
            CTEValidationWarning(field="carrier", message="soft nudge", severity="recommended"),
        ]
    )

    assert [(w.field, w.severity) for w in deduped] == [
        ("input_products", "required"),
        ("carrier", "recommended"),
    ]

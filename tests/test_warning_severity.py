"""Warnings must say whether they are blockers, not just describe themselves.

`CTEValidationWarning` used to distinguish a missing FSMA-required KDE from a
missing recommended one only inside the prose of its `message`, so nothing —
not the shift log, not the readiness banner, not an operator scanning the
console — could tell a gap that would fail live RegEngine ingest from a
nice-to-have. These tests pin the machine-readable `severity` that replaced
that, the required-first ordering every rendered list depends on, and the
message strings, which stay byte-identical so anything pinning them still
passes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.audit import summarize_scenario_audit
from app.cte_rules import (
    RECOMMENDED_KDES,
    REQUIRED_KDES,
    CTEValidationWarning,
    audit_warnings_for_event,
    dedupe_warnings,
    validate_event_kdes,
)
from app.demo_fixtures import DEMO_FIXTURES
from app.scenarios import ScenarioId, get_scenario
from app.schemas.domain import (
    CTEType,
    DemoFixtureId,
    RegEngineEvent,
    StoredEventRecord,
)


def _bare_harvest_event() -> RegEngineEvent:
    """A harvest event with no KDEs at all, so every gap shows up at once."""
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="TLC-SEVERITY-001",
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=datetime.now(UTC),
        kdes={},
    )


def test_missing_required_kdes_are_marked_required() -> None:
    warnings = validate_event_kdes(_bare_harvest_event())
    by_field = {warning.field: warning for warning in warnings}

    for field in REQUIRED_KDES[CTEType.HARVESTING]:
        if field in by_field:
            assert by_field[field].severity == "required", field


def test_missing_recommended_kdes_are_marked_recommended() -> None:
    warnings = validate_event_kdes(_bare_harvest_event())
    by_field = {warning.field: warning for warning in warnings}

    for field in RECOMMENDED_KDES[CTEType.HARVESTING]:
        assert field in by_field, field
        assert by_field[field].severity == "recommended", field


def test_severity_defaults_to_recommended() -> None:
    """An advisory warning must never be promoted to a blocker by accident."""
    assert CTEValidationWarning(field="x", message="y").severity == "recommended"


def test_required_warnings_sort_ahead_of_recommended_ones() -> None:
    severities = [warning.severity for warning in validate_event_kdes(_bare_harvest_event())]

    assert severities, "expected the bare event to produce warnings"
    assert severities == sorted(severities, key=lambda value: value != "required")

    scenario = get_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER)
    audit_severities = [
        warning.severity for warning in audit_warnings_for_event(_bare_harvest_event(), scenario)
    ]
    assert audit_severities == sorted(audit_severities, key=lambda value: value != "required")


def test_dedupe_keeps_one_copy_and_still_orders_required_first() -> None:
    recommended = CTEValidationWarning(field="a", message="a", severity="recommended")
    required = CTEValidationWarning(field="b", message="b", severity="required")

    assert dedupe_warnings([recommended, required, recommended]) == [required, recommended]


def test_warning_messages_are_unchanged_by_the_severity_field() -> None:
    """The strings are load-bearing elsewhere, so they stay byte-identical."""
    messages = {warning.field: warning.message for warning in validate_event_kdes(_bare_harvest_event())}

    assert messages["harvest_date"] == "Missing expected harvesting KDE: harvest_date"
    assert messages["field_name"] == "Missing recommended harvesting KDE: field_name"


def test_audit_summary_serialises_severity_and_counts_it_separately() -> None:
    scenario = get_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER)
    record = StoredEventRecord(payload_source="severity-test", event=_bare_harvest_event())

    summary = summarize_scenario_audit([record], scenario)
    payload = summary["warnings_by_record"][record.record_id]

    assert payload, "expected serialised warnings for a bare event"
    assert all("severity" in item for item in payload)
    assert [item["severity"] for item in payload][0] == "required"
    assert summary["required_warning_count"] > 0
    assert summary["records_with_required_warnings"] == 1
    assert (
        summary["required_warning_count"] + summary["recommended_warning_count"]
        == summary["warning_count"]
    )


def test_a_clean_event_reports_no_required_gaps() -> None:
    """The counts must be able to say "nothing would fail live ingest"."""
    scenario = get_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER)
    fixture = DEMO_FIXTURES[DemoFixtureId.LEAFY_GREENS_TRACE]
    records = [
        StoredEventRecord(payload_source="severity-test", event=fixture_event.event)
        for fixture_event in fixture.events
    ]

    summary = summarize_scenario_audit(records, scenario)

    assert summary["required_warning_count"] == 0
    assert summary["records_with_required_warnings"] == 0


def test_a_required_gap_suppresses_the_same_field_s_advisory() -> None:
    """The dedupe pass must not let a field's advisory outrank its required gap.

    A consumer that reads the first warning for a field -- which is what the
    shift log does, one row per warning -- would otherwise report
    "recommended" for something the required tier already flagged.

    Asserted directly on `dedupe_warnings` rather than through a scenario,
    deliberately. The two tiers are assembled independently and one field can
    already be named by both (`seafood_first_receiver` does it with
    `vessel_identifier`), but today both of those entries are "recommended",
    so no rule table currently produces the required/recommended clash this
    guards. Promoting any `EventRequirement` to "required" makes it live, and
    that is precisely the edit that would otherwise remove this behaviour
    without a single test noticing.
    """
    required = CTEValidationWarning(field="lot", message="required gap", severity="required")
    advisory = CTEValidationWarning(field="lot", message="advisory nudge", severity="recommended")
    other = CTEValidationWarning(field="other", message="unrelated", severity="recommended")

    deduped = dedupe_warnings([advisory, required, other])

    assert deduped == [required, other], deduped
    # The advisory for a DIFFERENT field is untouched -- this suppresses one
    # field's lower tier, not every recommended warning in the list.
    assert other in deduped

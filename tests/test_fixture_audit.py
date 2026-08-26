"""Regression coverage for shipped demo fixtures and scenario preset data.

Demo fixtures are the "this is what good looks like" path in the operator
console, so they must score exactly as well as a live simulation of the same
scenario on the console's own audit checks (issue #172). Scenario presets must
also keep GLNs globally unique per real-world location (issue #173).
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from app.audit import summarize_scenario_audit
from app.cte_rules import audit_warnings_for_event
from app.demo_fixtures import DEMO_FIXTURES
from app.engine import LegitFlowEngine
from app.scenarios import SCENARIO_PRESETS, get_scenario
from app.schemas.domain import StoredEventRecord

LOCATION_GROUPS = ("farms", "coolers", "packers", "processors", "dcs", "retailers")


def _fixture_records(fixture) -> list[StoredEventRecord]:
    return [
        StoredEventRecord(
            payload_source="fixture-audit-test",
            event=fixture_event.event,
            parent_lot_codes=list(fixture_event.parent_lot_codes),
        )
        for fixture_event in fixture.events
    ]


@pytest.mark.parametrize("fixture_id", sorted(DEMO_FIXTURES, key=lambda item: item.value))
def test_demo_fixture_scores_full_marks_on_audit_checks(fixture_id) -> None:
    fixture = DEMO_FIXTURES[fixture_id]
    scenario = get_scenario(fixture.scenario)

    summary = summarize_scenario_audit(_fixture_records(fixture), scenario)

    failed = [check["label"] for check in summary["checks"] if not check["ok"]]
    assert failed == [], f"{fixture_id.value} failed audit checks: {failed}"
    assert summary["passed"] == summary["total"]
    assert summary["score"] == 100
    assert summary["tone"] == "ready"


@pytest.mark.parametrize("fixture_id", sorted(DEMO_FIXTURES, key=lambda item: item.value))
def test_demo_fixture_events_emit_no_audit_warnings(fixture_id) -> None:
    fixture = DEMO_FIXTURES[fixture_id]
    scenario = get_scenario(fixture.scenario)

    offenders = {
        fixture_event.event.traceability_lot_code: [
            (warning.field, warning.message)
            for warning in audit_warnings_for_event(fixture_event.event, scenario)
        ]
        for fixture_event in fixture.events
        if audit_warnings_for_event(fixture_event.event, scenario)
    }

    assert offenders == {}


@pytest.mark.parametrize("fixture_id", sorted(DEMO_FIXTURES, key=lambda item: item.value))
def test_demo_fixture_reference_documents_match_scenario_format(fixture_id) -> None:
    fixture = DEMO_FIXTURES[fixture_id]
    scenario = get_scenario(fixture.scenario)
    if scenario.reference_format != "GS1":
        pytest.skip("scenario does not use GS1 reference documents")

    for fixture_event in fixture.events:
        reference_document = fixture_event.event.kdes.get("reference_document")
        assert reference_document, f"{fixture_id.value} event missing reference_document"
        assert reference_document.startswith("GS1"), reference_document


@pytest.mark.parametrize("scenario_id", sorted(SCENARIO_PRESETS, key=lambda item: item.value))
def test_scenario_preset_live_run_scores_full_marks(scenario_id) -> None:
    engine = LegitFlowEngine(seed=204, scenario=scenario_id)
    records = []
    for _ in range(160):
        event, parents = engine.next_event()
        records.append(
            StoredEventRecord(
                payload_source="fixture-audit-test",
                event=event,
                parent_lot_codes=list(parents),
            )
        )

    summary = summarize_scenario_audit(records, get_scenario(scenario_id))

    failed = [check["label"] for check in summary["checks"] if not check["ok"]]
    assert failed == [], f"{scenario_id.value} failed audit checks: {failed}"
    assert summary["score"] == 100


def test_demo_fixtures_match_their_scenarios_live_audit_score() -> None:
    """A canned fixture must never score worse than a live run of the same scenario."""
    for fixture in DEMO_FIXTURES.values():
        scenario = get_scenario(fixture.scenario)
        engine = LegitFlowEngine(seed=204, scenario=fixture.scenario)
        live_records = []
        for _ in range(160):
            event, parents = engine.next_event()
            live_records.append(
                StoredEventRecord(
                    payload_source="fixture-audit-test",
                    event=event,
                    parent_lot_codes=list(parents),
                )
            )

        fixture_summary = summarize_scenario_audit(_fixture_records(fixture), scenario)
        live_summary = summarize_scenario_audit(live_records, scenario)

        assert fixture_summary["score"] >= live_summary["score"]


def test_scenario_preset_glns_are_unique_per_location() -> None:
    """Each GLN must resolve to exactly one real-world facility.

    The produce trio intentionally shares facilities (e.g. "Valley Fresh Farms"
    appears in three scenarios), so identity is name + location_type, not the
    (scenario, location) pair.
    """
    by_gln: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for scenario in SCENARIO_PRESETS.values():
        for group in LOCATION_GROUPS:
            for location in getattr(scenario, group):
                by_gln[location.gln].add((location.name, location.location_type))

    collisions = {gln: sorted(names) for gln, names in by_gln.items() if len(names) > 1}

    assert collisions == {}


def test_shared_locations_keep_a_single_gln() -> None:
    """The inverse check: one facility must not be issued two different GLNs."""
    by_location: dict[tuple[str, str], set[str]] = defaultdict(set)
    for scenario in SCENARIO_PRESETS.values():
        for group in LOCATION_GROUPS:
            for location in getattr(scenario, group):
                by_location[(location.name, location.location_type)].add(location.gln)

    split = {key: sorted(glns) for key, glns in by_location.items() if len(glns) > 1}

    assert split == {}

"""Regression tests guarding demo-fixture and scenario-preset data quality.

Covers two GitHub issues:

- #172: the three shipped ``DemoFixture`` entries in ``app/demo_fixtures.py``
  must score at the audit-ready tier ``app/audit.py`` reports for a live,
  randomly-generated run of the same scenario -- not the worst possible
  score, which is what the hand-authored fixtures had drifted to. Fixtures
  are the "this is what good looks like" walkthrough; they should never
  read worse than messier randomly-generated data.
- #173: ``SCENARIO_PRESETS`` in ``app/scenarios.py`` must never let two
  differently-named locations resolve to the same GLN by copy-paste
  accident. A small, explicitly-documented set of GLNs *is* meant to be
  shared -- the leafy-greens / fresh-cut / retailer trio deliberately
  models a handful of real businesses (e.g. "Valley Fresh Farms") that
  supply all three demo personas -- and that set is asserted here by name
  so the distinction between "intentional" and "bug" stays visible.

These tests only read from ``app/demo_fixtures.py`` and ``app/scenarios.py``.
They never relax the checks in ``app/audit.py`` / ``app/cte_rules.py`` or the
KDE injection in ``app/industry_adapters.py`` -- fixture and preset data is
made to match what those already-correct modules expect, not the reverse.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

import pytest

from app.audit import summarize_scenario_audit
from app.demo_fixtures import DEMO_FIXTURES, DemoFixture, get_demo_fixture
from app.engine import LegitFlowEngine
from app.mock_service import MAX_EVENT_AGE_DAYS, validate_event_like_regengine
from app.scenarios import SCENARIO_PRESETS, ScenarioId, ScenarioPreset, _gln, get_scenario
from app.schemas.domain import StoredEventRecord

# A live run needs enough events to exercise harvest, cooling, packing,
# shipping, receiving, and transformation at least once each -- 120 matches
# the reproduction steps in issue #172.
_LIVE_EVENT_COUNT = 120
_LIVE_SEED = 204


def _fixture_records(fixture: DemoFixture) -> list[StoredEventRecord]:
    """Build StoredEventRecords the same way SimulationController.load_demo_fixture does."""
    return [
        StoredEventRecord(
            payload_source="test",
            event=fixture_event.event,
            parent_lot_codes=list(fixture_event.parent_lot_codes),
        )
        for fixture_event in fixture.events
    ]


def _live_records(scenario_id: ScenarioId) -> list[StoredEventRecord]:
    """Generate a live-engine run of ``scenario_id``, wrapped like stored records."""
    engine = LegitFlowEngine(seed=_LIVE_SEED, scenario=scenario_id)
    records = []
    for _ in range(_LIVE_EVENT_COUNT):
        event, parents = engine.next_event()
        records.append(
            StoredEventRecord(payload_source="test", event=event, parent_lot_codes=list(parents))
        )
    return records


@pytest.mark.parametrize("fixture", DEMO_FIXTURES.values(), ids=lambda f: f.id.value)
def test_fixture_audit_score_matches_its_live_generated_equivalent(fixture: DemoFixture) -> None:
    """A demo fixture must not show the worst possible audit score.

    Regression test for #172: every shipped fixture scored 0/3 checks (0%,
    "Developing") while a live run of the same scenario scored 100
    ("Audit-ready pattern"). Assert the fixture reaches the same audit-ready
    tier as its own live-generated equivalent, with every check passing and
    no warnings left over -- exactly what the live run already achieves.
    """
    scenario = get_scenario(fixture.scenario)

    fixture_summary = summarize_scenario_audit(_fixture_records(fixture), scenario)
    live_summary = summarize_scenario_audit(_live_records(fixture.scenario), scenario)

    # Sanity check on the comparison point itself: the live engine really
    # does reach the audit-ready tier for this scenario, so "matches its
    # live equivalent" below is a meaningful bar, not a vacuous one.
    assert live_summary["tone"] == "ready"
    assert live_summary["score"] == 100

    assert fixture_summary["tone"] == "ready", (
        f"{fixture.id.value} audit tone was {fixture_summary['tone']!r} "
        f"(score {fixture_summary['score']}, label {fixture_summary['label']!r}); "
        f"expected 'ready' to match its live-generated equivalent"
    )
    assert fixture_summary["passed"] == fixture_summary["total"], (
        f"{fixture.id.value} failed audit checks: "
        f"{[c['label'] for c in fixture_summary['checks'] if not c['ok']]}"
    )
    assert fixture_summary["score"] == 100
    assert fixture_summary["warning_count"] == 0, (
        f"{fixture.id.value} still raised audit warnings: {fixture_summary['warnings_by_record']}"
    )


@pytest.mark.parametrize("fixture", DEMO_FIXTURES.values(), ids=lambda f: f.id.value)
def test_fixture_events_pass_regengine_ingest_validation(fixture: DemoFixture) -> None:
    """Every fixture event must stay ingest-valid as its KDEs evolve.

    #172's fix adds KDEs (field_gps_coordinates, plu_code,
    packaging_hierarchy, packaging_conversion) and rewrites
    reference_document strings; neither change should ever cause a fixture
    event to fail RegEngine's own webhook validation (mirrored here by
    validate_event_like_regengine).
    """
    for fixture_event in fixture.events:
        errors = validate_event_like_regengine(fixture_event.event)
        assert errors == [], (
            f"{fixture.id.value} / {fixture_event.event.traceability_lot_code} "
            f"({fixture_event.event.cte_type.value}): {errors}"
        )


# ---------------------------------------------------------------------------
# #173 -- GLN uniqueness across SCENARIO_PRESETS.
#
# _gln() is a pure function of a small integer id, so copy-pasting an id
# range from one scenario into another silently makes two unrelated
# facilities "share" a GS1 location identifier. The leafy-greens /
# fresh-cut / retailer trio is the one deliberate exception: those three
# scenarios intentionally model a handful of the same real businesses (the
# same farm/cooler/packer/processor/retail sites feeding all three demo
# personas), so the same GLN legitimately recurs there with the same
# location name attached every time.
# ---------------------------------------------------------------------------

_PRODUCE_TRIO = (
    ScenarioId.LEAFY_GREENS_SUPPLIER,
    ScenarioId.FRESH_CUT_PROCESSOR,
    ScenarioId.RETAILER_READINESS_DEMO,
)

# location_id -> (location_name, scenarios that intentionally reuse it).
# Every id here is one of the hand-authored ids shared across the produce
# trio; _gln() computes the actual 13-digit GLN so no digit is hand-typed
# in this file, per #173's fix guidance.
_INTENTIONALLY_SHARED_LOCATION_IDS: dict[int, tuple[str, tuple[ScenarioId, ...]]] = {
    1001: ("Valley Fresh Farms", _PRODUCE_TRIO),
    1002: ("Desert Bloom Farm", (ScenarioId.LEAFY_GREENS_SUPPLIER, ScenarioId.FRESH_CUT_PROCESSOR)),
    1003: ("Riverbend Organics", (ScenarioId.LEAFY_GREENS_SUPPLIER, ScenarioId.RETAILER_READINESS_DEMO)),
    2001: ("Salinas Cooling Hub", _PRODUCE_TRIO),
    3001: ("FreshPack Central", (ScenarioId.LEAFY_GREENS_SUPPLIER, ScenarioId.RETAILER_READINESS_DEMO)),
    3002: ("GreenLeaf Packing House", (ScenarioId.LEAFY_GREENS_SUPPLIER, ScenarioId.FRESH_CUT_PROCESSOR)),
    4001: ("ReadyFresh Processing Plant", _PRODUCE_TRIO),
    6001: ("Retail Store #4521", _PRODUCE_TRIO),
    6002: ("Retail Store #3189", (ScenarioId.LEAFY_GREENS_SUPPLIER, ScenarioId.RETAILER_READINESS_DEMO)),
}

INTENTIONALLY_SHARED_GLNS: dict[str, tuple[str, tuple[ScenarioId, ...]]] = {
    _gln(location_id): value for location_id, value in _INTENTIONALLY_SHARED_LOCATION_IDS.items()
}


def _gln_usage() -> dict[str, set[tuple[ScenarioId, str]]]:
    """Map every GLN in SCENARIO_PRESETS to the (scenario, location name) pairs using it."""
    usage: dict[str, set[tuple[ScenarioId, str]]] = defaultdict(set)
    for scenario_id, preset in SCENARIO_PRESETS.items():
        for tier in (
            preset.farms,
            preset.coolers,
            preset.packers,
            preset.processors,
            preset.dcs,
            preset.retailers,
        ):
            for location in tier:
                usage[location.gln].add((scenario_id, location.name))
    return usage


def test_no_gln_resolves_to_more_than_one_location_name() -> None:
    """Regression test for #173: a GLN must never mean two different businesses.

    copacker_nut_butter's farm ids collided with dairy_continuous_flow's, and
    its cooler/packer/processor/dc/retailer ids collided 1:1 with
    seafood_first_receiver's -- an almond ranch "sharing" a GLN with a dairy
    farm, a fish-landing dock "sharing" one with a nut-butter production
    line. Any GLN mapping to more than one distinct location name is that
    bug, full stop, regardless of which scenarios are involved -- this check
    intentionally has no exceptions list.
    """
    usage = _gln_usage()
    offenders = {
        gln: sorted({name for _, name in pairs})
        for gln, pairs in usage.items()
        if len({name for _, name in pairs}) > 1
    }
    assert offenders == {}, f"GLNs resolving to more than one location name: {offenders}"


def test_gln_sharing_across_scenarios_is_limited_to_the_documented_produce_trio() -> None:
    """A GLN may recur across scenarios only for the documented, intentional reasons.

    Every GLN used by more than one scenario must appear in
    INTENTIONALLY_SHARED_GLNS with exactly the recorded scenario set and
    location name, so any *new* cross-scenario GLN reuse -- accidental or
    otherwise -- fails loudly here instead of shipping silently. This is the
    check that would have caught #173 before it shipped.
    """
    usage = _gln_usage()

    for gln, pairs in usage.items():
        scenarios_using_it = {scenario_id for scenario_id, _ in pairs}
        if len(scenarios_using_it) <= 1:
            continue
        assert gln in INTENTIONALLY_SHARED_GLNS, (
            f"GLN {gln} is unexpectedly shared across scenarios "
            f"{sorted(s.value for s in scenarios_using_it)}: {sorted(pairs)}"
        )
        expected_name, expected_scenarios = INTENTIONALLY_SHARED_GLNS[gln]
        assert scenarios_using_it == set(expected_scenarios), (
            f"GLN {gln} ({expected_name}) is shared by "
            f"{sorted(s.value for s in scenarios_using_it)}, expected exactly "
            f"{sorted(s.value for s in expected_scenarios)}"
        )
        assert {name for _, name in pairs} == {expected_name}

    # And the reverse direction: every documented share must still be real,
    # so this list can't silently go stale as scenarios change.
    for gln, (expected_name, expected_scenarios) in INTENTIONALLY_SHARED_GLNS.items():
        assert gln in usage, f"expected shared GLN {gln} ({expected_name}) not used by any scenario"
        assert {scenario_id for scenario_id, _ in usage[gln]} == set(expected_scenarios)


def test_copacker_nut_butter_no_longer_collides_with_dairy_or_seafood() -> None:
    """Directly encodes #173's acceptance criteria for the offending scenario."""
    copacker = SCENARIO_PRESETS[ScenarioId.COPACKER_NUT_BUTTER]
    dairy = SCENARIO_PRESETS[ScenarioId.DAIRY_CONTINUOUS_FLOW]
    seafood = SCENARIO_PRESETS[ScenarioId.SEAFOOD_FIRST_RECEIVER]

    copacker_farm_glns = {location.gln for location in copacker.farms}
    dairy_farm_glns = {location.gln for location in dairy.farms}
    assert not copacker_farm_glns & dairy_farm_glns, "copacker farm GLNs still collide with dairy's"

    def _non_farm_glns(preset: ScenarioPreset) -> set[str]:
        return {
            location.gln
            for tier in (preset.coolers, preset.packers, preset.processors, preset.dcs, preset.retailers)
            for location in tier
        }

    assert not _non_farm_glns(copacker) & _non_farm_glns(seafood), (
        "copacker cooler/packer/processor/dc/retailer GLNs still collide with seafood's"
    )


# ---------------------------------------------------------------------------
# #199 -- load-time rebasing keeps the fixtures inside the replay window
#         without deforming the walkthrough they exist to tell
# ---------------------------------------------------------------------------


def test_loaded_fixtures_land_inside_the_replay_window() -> None:
    """The point of rebasing: Load Demo Fixture must not post stale events.

    RegEngine rejects anything older than MAX_EVENT_AGE_DAYS, and the mock now
    enforces the same floor by default, so a fixture frozen at its authored
    date would fail the one walkthrough the fixtures exist to make reliable --
    and would fail a day harder every day.
    """
    now = datetime.now(UTC)
    for fixture_id in DEMO_FIXTURES:
        for fixture_event in get_demo_fixture(fixture_id).events:
            age = (now - fixture_event.event.timestamp).days
            assert 0 <= age < MAX_EVENT_AGE_DAYS, (
                f"{fixture_id.value} event {fixture_event.event.traceability_lot_code} "
                f"is {age} days old; the window is {MAX_EVENT_AGE_DAYS} days and "
                "nothing may be in the future either"
            )


def test_rebasing_preserves_spacing_time_of_day_and_cross_fixture_order() -> None:
    """A shift, not a rewrite. The fixtures are a lineage narrative: harvest
    then cool then pack, hours apart, in a fixed order across all three. A
    per-event or per-fixture adjustment would land them in the window while
    destroying the story, so the offset is one whole number of days applied
    to the whole set.
    """
    for fixture_id, original in DEMO_FIXTURES.items():
        rebased = get_demo_fixture(fixture_id)
        authored = [event.event.timestamp for event in original.events]
        loaded = [event.event.timestamp for event in rebased.events]

        assert [
            loaded[i + 1] - loaded[i] for i in range(len(loaded) - 1)
        ] == [authored[i + 1] - authored[i] for i in range(len(authored) - 1)], (
            f"{fixture_id.value}: rebasing changed the spacing between events"
        )
        assert [stamp.timetz() for stamp in loaded] == [stamp.timetz() for stamp in authored], (
            f"{fixture_id.value}: rebasing moved events to a different time of day"
        )

    def earliest(getter):
        return [
            fixture_id.value
            for fixture_id, _ in sorted(
                (
                    (fixture_id, min(event.event.timestamp for event in getter(fixture_id).events))
                    for fixture_id in DEMO_FIXTURES
                ),
                key=lambda pair: pair[1],
            )
        ]

    assert earliest(lambda fixture_id: DEMO_FIXTURES[fixture_id]) == earliest(get_demo_fixture), (
        "rebasing reordered the fixtures relative to each other"
    )


def test_bare_date_kdes_move_with_the_event_they_describe() -> None:
    """A shifted timestamp and a frozen date KDE is a self-contradicting record.

    harvest_date, cooling_date, pack_date, ship_date, receive_date,
    transformation_date and landing_date all restate their event's own day. If
    the timestamp moves and they do not, the exported row says the lot was
    harvested on one date and the event happened on another -- and both the
    FDA CSV and the validators read those KDEs, so the contradiction is
    visible to exactly the audience the fixtures are for.

    Asserted against the event's OWN day rather than a fixed expected value,
    so it holds whenever the suite runs.
    """
    date_kdes = {
        "harvest_date",
        "cooling_date",
        "pack_date",
        "packing_date",
        "ship_date",
        "receive_date",
        "transformation_date",
        "landing_date",
    }
    checked = 0
    for fixture_id in DEMO_FIXTURES:
        for fixture_event in get_demo_fixture(fixture_id).events:
            event_day = fixture_event.event.timestamp.date().isoformat()
            for key, value in fixture_event.event.kdes.items():
                if key in date_kdes and isinstance(value, str):
                    checked += 1
                    assert value == event_day, (
                        f"{fixture_id.value}: {key}={value!r} but the event is on "
                        f"{event_day} -- the KDE did not move with its timestamp"
                    )
    assert checked, "no bare-date KDEs were found to check"

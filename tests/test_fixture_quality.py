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
from datetime import UTC, datetime, timedelta

import pytest

from app.audit import summarize_scenario_audit
from app.demo_fixtures import (
    DEMO_FIXTURES,
    FIXTURE_RECENCY_DAYS,
    DemoFixture,
    get_demo_fixture,
)
from app.engine import LegitFlowEngine
from app.mock_service import (
    MAX_EVENT_AGE_DAYS,
    MockRegEngineService,
    validate_event_like_regengine,
)
from app.scenarios import SCENARIO_PRESETS, ScenarioId, ScenarioPreset, _gln, get_scenario
from app.schemas.domain import StoredEventRecord
from app.schemas.ingestion import IngestPayload

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
# #199 -- shipped fixtures must stay inside RegEngine's 90-day replay window.
#
# The fixture literals in app/demo_fixtures.py are fixed dates (2026-02-05
# and friends), which the golden export tests depend on. What must not be
# fixed is how old they are when *loaded*: live ingest rejects anything older
# than MAX_EVENT_AGE_DAYS, so a literal date silently rots by one day every
# day until the Load Demo Fixture walkthrough fails wholesale against a real
# RegEngine -- while the mock keeps accepting it. get_demo_fixture() rebases
# onto the current date, so these assert the property at load time and at an
# arbitrary future run date.
# ---------------------------------------------------------------------------

_FIXTURE_IDS = tuple(DEMO_FIXTURES)


@pytest.mark.parametrize("fixture_id", _FIXTURE_IDS, ids=lambda f: f.value)
def test_loaded_fixture_events_are_inside_the_replay_window(fixture_id) -> None:
    now = datetime.now(UTC)
    fixture = get_demo_fixture(fixture_id)

    for fixture_event in fixture.events:
        age = now - fixture_event.event.timestamp
        assert age < timedelta(days=MAX_EVENT_AGE_DAYS), (
            f"{fixture_id.value} / {fixture_event.event.traceability_lot_code} is "
            f"{age.days} days old; live ingest rejects past {MAX_EVENT_AGE_DAYS}"
        )
        # Not in the future either: both the mock and live reject events past
        # a small future ceiling, so rebasing must not overshoot.
        assert fixture_event.event.timestamp <= now


@pytest.mark.parametrize("years_from_now", [1, 5, 50], ids=["1y", "5y", "50y"])
@pytest.mark.parametrize("fixture_id", _FIXTURE_IDS, ids=lambda f: f.value)
def test_fixtures_cannot_silently_rot_at_a_future_run_date(fixture_id, years_from_now) -> None:
    """The anti-rot guarantee: the window property holds whenever the repo is
    run, not just on the day the fixtures were written.
    """
    future_now = datetime.now(UTC) + timedelta(days=365 * years_from_now)
    fixture = get_demo_fixture(fixture_id, now=future_now)

    for fixture_event in fixture.events:
        age = future_now - fixture_event.event.timestamp
        assert timedelta(0) <= age < timedelta(days=MAX_EVENT_AGE_DAYS)


@pytest.mark.parametrize("fixture_id", _FIXTURE_IDS, ids=lambda f: f.value)
def test_rebasing_preserves_relative_spacing_and_kde_dates(fixture_id) -> None:
    """The lineage narrative is carried by the *gaps* between events, so the
    rebased copy has to be a rigid translation of the original -- and any
    bare-date KDE has to move with the timestamp it describes, or a record
    starts contradicting itself (a harvest_date of 2026-02-05 on an event
    stamped last Tuesday).
    """
    original = DEMO_FIXTURES[fixture_id]
    rebased = get_demo_fixture(fixture_id)

    assert len(rebased.events) == len(original.events)

    shifts = {
        rebased_event.event.timestamp - original_event.event.timestamp
        for original_event, rebased_event in zip(original.events, rebased.events, strict=True)
    }
    # Exactly one offset across every event == spacing and ordering preserved.
    assert len(shifts) == 1
    (shift,) = shifts
    assert shift.seconds == 0, "whole days only, so each event keeps its time of day"

    for original_event, rebased_event in zip(original.events, rebased.events, strict=True):
        assert rebased_event.parent_lot_codes == original_event.parent_lot_codes
        assert (
            rebased_event.event.traceability_lot_code
            == original_event.event.traceability_lot_code
        )
        for key, original_value in original_event.event.kdes.items():
            rebased_value = rebased_event.event.kdes[key]
            if key.endswith("_date"):
                assert (
                    datetime.fromisoformat(rebased_value).date()
                    == datetime.fromisoformat(original_value).date() + shift
                ), key
            else:
                assert rebased_value == original_value, key


@pytest.mark.parametrize("fixture_id", _FIXTURE_IDS, ids=lambda f: f.value)
def test_loaded_fixtures_pass_ingest_with_the_age_window_enforced(fixture_id) -> None:
    """Acceptance criterion: ``enforce_event_age_window=True`` can be turned on
    without rejecting fixture events. That flag has to stay off by default for
    now only because dozens of *other* tests still use the fixed 2026-02-05
    timestamp as their canonical valid event (#102's remaining half) -- the
    fixtures themselves no longer stand in the way.
    """
    now = datetime.now(UTC)
    fixture = get_demo_fixture(fixture_id, now=now)

    for fixture_event in fixture.events:
        assert validate_event_like_regengine(fixture_event.event, now=now) == [], (
            f"{fixture_id.value} / {fixture_event.event.traceability_lot_code}"
        )

    service = MockRegEngineService(enforce_event_age_window=True)
    response = service.ingest(
        IngestPayload(
            source="fixture-window-check",
            events=[fixture_event.event for fixture_event in fixture.events],
        )
    )
    assert [event.status for event in response.events] == ["accepted"] * len(fixture.events)


def test_engine_generated_events_also_pass_with_the_age_window_enforced() -> None:
    """The other half of the same acceptance criterion. The engine anchors its
    clock to ``now - 12h``, so this should already hold -- pinned so a future
    change to the engine's time cursor cannot quietly break the flag.
    """
    now = datetime.now(UTC)
    engine = LegitFlowEngine(seed=_LIVE_SEED, scenario=ScenarioId.LEAFY_GREENS_SUPPLIER)
    events = [engine.next_event()[0] for _ in range(_LIVE_EVENT_COUNT)]

    for event in events:
        assert validate_event_like_regengine(event, now=now) == [], event.traceability_lot_code


@pytest.mark.parametrize("fixture_id", _FIXTURE_IDS, ids=lambda f: f.value)
def test_rebasing_does_not_regress_the_audit_score(fixture_id) -> None:
    """#172's fix made all three fixtures score 100%. Rebasing must not undo
    that -- the audit reads KDEs, and the KDE dates move during a rebase.
    """
    fixture = get_demo_fixture(fixture_id)
    summary = summarize_scenario_audit(_fixture_records(fixture), get_scenario(fixture.scenario))

    assert summary["tone"] == "ready"
    assert summary["score"] == 100
    assert summary["warning_count"] == 0


def test_the_newest_fixture_event_lands_where_the_rebase_intends() -> None:
    """Pins FIXTURE_RECENCY_DAYS' contract: the set is anchored by its newest
    event across all three fixtures, so their order relative to each other is
    preserved too.
    """
    now = datetime.now(UTC)
    newest = max(
        fixture_event.event.timestamp
        for fixture_id in _FIXTURE_IDS
        for fixture_event in get_demo_fixture(fixture_id, now=now).events
    )

    assert newest.date() == (now - timedelta(days=FIXTURE_RECENCY_DAYS)).date()

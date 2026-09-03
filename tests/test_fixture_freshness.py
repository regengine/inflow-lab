"""Nothing we ship may fall out of RegEngine's replay window.

RegEngine rejects any event older than ``WEBHOOK_MAX_EVENT_AGE_DAYS`` (90)
with "replay window exceeded", and anything more than 24h in the future is a
request-fatal 422. The demo fixtures shipped with fixed 2026-02 timestamps
for months, which quietly made the design-partner demo unreplayable against a
live tenant and forced the mock's age enforcement down to "warn" (#102).

These tests are the guard that stops that recurring: they fail while the
staleness is still a test failure rather than a demo failure. They cover
everything with a timestamp that we ship — canned demo fixtures, live runs of
every scenario preset, and the browser smoke's import CSV — and they fail
with margin to spare, so there is time to react before the demo actually
breaks.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.demo_fixtures import (
    DEMO_FIXTURES,
    FIXTURE_TIME_SHIFT,
    MAX_FIXTURE_AGE_DAYS,
    FIXTURE_BASE_DATE,
    fixture_time_shift,
)
from app.engine import LegitFlowEngine
from app.mock_service import MAX_FUTURE_HOURS, max_event_age_days, replay_window_errors
from app.scenarios import SCENARIO_PRESETS
from scripts.browser_smoke import import_csv_with_kde_warnings

#: How much of the replay window shipped data must keep in reserve. An event
#: at 89 days is technically valid and one deploy away from being invalid, so
#: "inside the window" is not a strong enough assertion on its own.
FRESHNESS_MARGIN_DAYS = 30


def _shipped_events():
    for fixture in DEMO_FIXTURES.values():
        for fixture_event in fixture.events:
            yield fixture.id.value, fixture_event.event


def _live_events(scenario_id, count: int = 120):
    engine = LegitFlowEngine(seed=204, scenario=scenario_id)
    for _ in range(count):
        event, _parents = engine.next_event()
        yield event


@pytest.mark.parametrize("fixture_id", sorted(DEMO_FIXTURES, key=lambda item: item.value))
def test_demo_fixture_events_sit_inside_the_replay_window(fixture_id) -> None:
    offenders = [
        (event.traceability_lot_code, event.timestamp.isoformat(), replay_window_errors(event))
        for name, event in _shipped_events()
        if name == fixture_id.value and replay_window_errors(event)
    ]

    assert offenders == [], f"{fixture_id.value} ships out-of-window events: {offenders}"


@pytest.mark.parametrize("fixture_id", sorted(DEMO_FIXTURES, key=lambda item: item.value))
def test_demo_fixture_events_keep_margin_and_are_not_future_dated(fixture_id) -> None:
    """Inside the window with room to spare, and never ahead of the clock."""
    now = datetime.now(UTC)
    floor = now - timedelta(days=max_event_age_days() - FRESHNESS_MARGIN_DAYS)
    ceiling = now + timedelta(hours=MAX_FUTURE_HOURS)

    for name, event in _shipped_events():
        if name != fixture_id.value:
            continue
        assert event.timestamp >= floor, (
            f"{name} event {event.traceability_lot_code} at {event.timestamp.isoformat()} is "
            f"within {FRESHNESS_MARGIN_DAYS} days of the replay-window floor — rebase the "
            "fixtures onto 'now' before the demo breaks"
        )
        assert event.timestamp <= ceiling, (
            f"{name} event {event.traceability_lot_code} is future-dated past the "
            f"{MAX_FUTURE_HOURS}h ceiling"
        )


@pytest.mark.parametrize("scenario_id", sorted(SCENARIO_PRESETS, key=lambda item: item.value))
def test_scenario_preset_live_run_stays_inside_the_replay_window(scenario_id) -> None:
    offenders = [
        (event.traceability_lot_code, event.timestamp.isoformat())
        for event in _live_events(scenario_id)
        if replay_window_errors(event)
    ]

    assert offenders == [], f"{scenario_id.value} generated out-of-window events: {offenders}"


def test_browser_smoke_import_csv_is_inside_the_replay_window() -> None:
    rows = list(csv.DictReader(io.StringIO(import_csv_with_kde_warnings())))

    assert rows, "browser smoke import CSV lost its data row"
    now = datetime.now(UTC)
    for row in rows:
        moment = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        age = now - moment
        assert timedelta(0) <= age <= timedelta(
            days=max_event_age_days() - FRESHNESS_MARGIN_DAYS
        ), f"browser smoke CSV row is stale or future-dated: {row['timestamp']}"


def test_fixture_rebasing_preserves_relative_spacing_and_order() -> None:
    """The lineage story is the sequence, so the shift must be rigid."""
    for fixture in DEMO_FIXTURES.values():
        timestamps = [fixture_event.event.timestamp for fixture_event in fixture.events]
        assert timestamps == sorted(timestamps), f"{fixture.id.value} events are out of order"

    # Spacing is preserved because every event moved by the same whole-day
    # offset; a shift that was not rigid would show up as a changed gap.
    leafy = DEMO_FIXTURES[next(iter(DEMO_FIXTURES))]
    gaps = [
        later.event.timestamp - earlier.event.timestamp
        for earlier, later in zip(leafy.events, leafy.events[1:])
    ]
    assert all(gap > timedelta(0) for gap in gaps)
    assert FIXTURE_TIME_SHIFT == timedelta(days=FIXTURE_TIME_SHIFT.days), (
        "the fixture shift must be a whole number of days so date-only KDEs "
        "cannot drift away from their event timestamps across midnight"
    )


def test_date_only_kdes_shift_with_their_event_timestamp() -> None:
    """harvest_date and friends must still name the day the event happened."""
    mismatches = []
    for name, event in _shipped_events():
        event_date = event.timestamp.date().isoformat()
        for key, value in event.kdes.items():
            if not isinstance(value, str) or not key.endswith("_date"):
                continue
            if value != event_date:
                mismatches.append((name, event.traceability_lot_code, key, value, event_date))

    assert mismatches == []


def test_fixture_shift_tracks_the_engines_own_history_offset() -> None:
    """A fixture run must look like a live run, not a differently aged one."""
    now = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)
    shift = fixture_time_shift(now=now)

    oldest = datetime(2026, 2, 5, 8, 0, tzinfo=UTC) + shift
    # REGENGINE_SIM_HISTORY_HOURS defaults to 336h (14 days), which is where
    # LegitFlowEngine.reset starts its own clock.
    assert timedelta(days=13) <= now - oldest <= timedelta(days=15)


def test_a_large_history_setting_cannot_age_the_fixtures_out_of_the_window(monkeypatch) -> None:
    """REGENGINE_SIM_HISTORY_HOURS is configurable; the window is not."""
    monkeypatch.setenv("REGENGINE_SIM_HISTORY_HOURS", str(336 * 20))
    now = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)

    oldest = datetime.combine(FIXTURE_BASE_DATE, datetime.min.time(), tzinfo=UTC) + timedelta(
        hours=8
    ) + fixture_time_shift(now=now)

    # +1 day of slack: the shift is whole days, so the authored 08:00 time of
    # day can sit either side of "now"'s time of day.
    assert now - oldest <= timedelta(days=MAX_FIXTURE_AGE_DAYS + 1)
    assert now - oldest < timedelta(days=max_event_age_days())

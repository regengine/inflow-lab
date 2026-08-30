"""The simulated clock must not pile events onto one instant, or fill silently.

Regression cover for #119. `_advance_time()` used to clamp the *cursor* to
`now + REGENGINE_SIM_MAX_FUTURE_HOURS`. The cursor starts 12h behind real time
and each event advances it by 10-240 simulated minutes, so a default-config run
reaches that ceiling within about 30 events. From then on every call added
minutes and clamped straight back down to a ceiling that had itself only moved
by however long the last call took. Measured on the unfixed engine, gaps fell
from ~2900s to ~3e-05s by event 30.

Two things are pinned here, and it is worth being precise about which is which:

* **The clamp is gone.** Capping the step instead of the cursor means the
  sequence stays strictly increasing even when the wall clock does not advance
  between two events. Under the old code both events landed on the ceiling,
  i.e. on the same instant exactly. `test_timestamps_advance_even_when_the_
  clock_does_not` is the test that actually distinguishes the two.

* **The collapse is no longer silent, and is now avoidable.** Spacing narrowing
  once the window is full is not a bug that can be coded away: a window of
  bounded width cannot hold an unbounded number of hours-apart events, so once
  it is full the only room left is whatever real time has added. What was wrong
  was that this happened with no signal and no lever. There is now a warning
  and `REGENGINE_SIM_LOOKBACK_HOURS`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import app.engine as engine_module
from app.engine import DEFAULT_LOOKBACK_HOURS, LegitFlowEngine, _max_future_hours


def _timestamps(count: int) -> list[datetime]:
    engine = LegitFlowEngine(seed=204)
    return [engine.next_event()[0].timestamp for _ in range(count)]


def _gaps(stamps: list[datetime]) -> list[float]:
    return [(later - earlier).total_seconds() for earlier, later in zip(stamps, stamps[1:])]


class _FrozenDatetime(datetime):
    """A `datetime` whose `now()` never advances.

    The engine reads the ceiling from `datetime.now(UTC)` on every call, so
    freezing it reproduces the case the old clamp collapsed: the window is full
    and no real time passes between two events.
    """

    _frozen = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls._frozen if tz is None else cls._frozen.astimezone(tz)


def test_timestamps_advance_even_when_the_clock_does_not(monkeypatch):
    # The decisive test. With `now()` frozen the window can never grow, so the
    # old code clamped every event past saturation onto the identical ceiling.
    monkeypatch.setattr(engine_module, "datetime", _FrozenDatetime)

    engine = LegitFlowEngine(seed=204)
    stamps = [engine.next_event()[0].timestamp for _ in range(500)]

    assert len(set(stamps)) == len(stamps), "events landed on the same instant"
    assert all(gap > 0 for gap in _gaps(stamps)), "timestamps did not strictly increase"


def test_timestamps_are_strictly_increasing_in_a_tight_loop():
    stamps = _timestamps(3000)

    assert len(set(stamps)) == len(stamps)
    assert all(gap > 0 for gap in _gaps(stamps))


def test_no_timestamp_escapes_the_live_window():
    # Capping the step rather than the cursor must not let events drift past the
    # ceiling that keeps them acceptable to live ingest. The ceiling is read
    # after the run because it moves forward with real time while the run is in
    # progress; each event was bounded by the (earlier, lower) ceiling in force
    # when it was generated, so this is a sound check on all of them.
    stamps = _timestamps(3000)
    ceiling = datetime.now(UTC) + timedelta(hours=_max_future_hours())

    assert max(stamps) <= ceiling


def test_filling_the_window_is_reported(caplog):
    # It used to happen silently, which is why a run could look healthy while
    # every timestamp it produced was microseconds from the last.
    with caplog.at_level(logging.WARNING, logger="app.engine"):
        _timestamps(200)

    saturation = [r for r in caplog.records if "Simulated-time window is full" in r.message]
    assert len(saturation) == 1, "expected exactly one saturation warning per engine"
    assert "REGENGINE_SIM_LOOKBACK_HOURS" in saturation[0].getMessage()


def test_early_events_keep_realistic_spacing():
    assert all(gap >= 10 * 60 for gap in _gaps(_timestamps(10)))


def test_a_wider_window_keeps_a_long_run_realistic(monkeypatch):
    # The width of the window, not `_advance_time`, decides how many hours-apart
    # events a run can produce. At the 12h default a 200-event run saturates
    # after roughly 30; given room, the same run stays realistic throughout.
    monkeypatch.setenv("REGENGINE_SIM_LOOKBACK_HOURS", str(24 * 60))

    stamps = _timestamps(200)

    assert min(_gaps(stamps)) >= 10 * 60, f"smallest gap {min(_gaps(stamps))}s"
    assert max(stamps) <= datetime.now(UTC) + timedelta(hours=_max_future_hours())


def test_a_wider_window_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="app.engine"):
        _timestamps(10)

    assert not [r for r in caplog.records if "Simulated-time window is full" in r.message]


def test_lookback_falls_back_to_the_default_on_a_bad_value(monkeypatch):
    monkeypatch.setenv("REGENGINE_SIM_LOOKBACK_HOURS", "not-a-number")
    engine = LegitFlowEngine(seed=204)

    assert engine.next_event()[0].timestamp >= datetime.now(UTC) - timedelta(
        hours=DEFAULT_LOOKBACK_HOURS + 1
    )

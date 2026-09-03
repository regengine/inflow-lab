"""Regression tests for engine correctness fixes (issues #117, #119).

- #117: `_make_sscc` truncated an 18-character string to 17 before hashing
  it, always dropping the serial's last digit and colliding distinct lots
  onto the same SSCC.
- #119: `_advance_time` clamped the simulated clock straight to a live
  wall-clock ceiling once reached, collapsing many events onto the same
  timestamp instead of keeping the trace strictly ordered.

See app/engine.py::_make_sscc and app/engine.py::_advance_time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import app.engine as engine_module
from app.engine import LegitFlowEngine
from app.schemas.domain import CTEType
from app.scenarios import _gln


def _assert_valid_sscc(engine: LegitFlowEngine, value: str | None) -> None:
    """An SSCC is 18 digits: a 17-digit payload plus a GS1 mod-10 check digit."""
    assert value is not None
    assert len(value) == 18
    assert value.isdigit()
    base, check_digit = value[:17], value[17]
    assert int(check_digit) == engine._gs1_check_digit(base)


# ---------------------------------------------------------------------------
# #117 - _make_sscc truncation / collisions
# ---------------------------------------------------------------------------


def test_make_sscc_is_eighteen_digits_with_valid_check_digit():
    engine = LegitFlowEngine(seed=204)
    for _ in range(25):
        _assert_valid_sscc(engine, engine._make_sscc())


def test_make_sscc_check_digit_matches_repos_gln_convention():
    # app/scenarios.py's `_gln` is the repo's existing GS1 mod-10 check
    # digit algorithm (used for every location GLN). `_make_sscc` reuses
    # `_gs1_check_digit`, which must compute check digits the same way
    # rather than a second, possibly-inconsistent algorithm.
    engine = LegitFlowEngine(seed=204)
    for location_id in (1001, 2001, 3001, 4001, 5001, 6001, 60000, 99999):
        gln = _gln(location_id)
        body, expected_check_digit = gln[:-1], gln[-1]
        assert str(engine._gs1_check_digit(body)) == expected_check_digit


def test_make_sscc_unique_across_large_sequential_run():
    # Calls _make_sscc() directly, bypassing the rest of the engine, so this
    # isolates the fix from any noise in how often shipping/landing events
    # happen to occur in a full run.
    engine = LegitFlowEngine(seed=204)
    sscc_values = [engine._make_sscc() for _ in range(500)]

    assert len(sscc_values) == len(set(sscc_values))
    for value in sscc_values:
        _assert_valid_sscc(engine, value)


def test_sscc_unique_across_shipments_in_a_full_engine_run():
    # Default scenario (leafy_greens_supplier) uses reference_format="GS1",
    # so every shipping event's BOL reference routes through _make_sscc().
    engine = LegitFlowEngine(seed=204)
    shipping_sscc: list[str] = []

    for _ in range(600):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.SHIPPING:
            shipping_sscc.append(event.kdes["sscc"])

    assert len(shipping_sscc) >= 100
    assert len(shipping_sscc) == len(set(shipping_sscc))
    for value in shipping_sscc:
        _assert_valid_sscc(engine, value)


def test_sscc_unique_across_land_based_receiving_in_seafood_scenario():
    # Exercises the other GS1 prefix named in the issue ("LAND"):
    # FIRST_LAND_BASED_RECEIVING events also route through _make_sscc().
    engine = LegitFlowEngine(seed=204, scenario="seafood_first_receiver")
    land_refs: list[str] = []

    for _ in range(400):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.FIRST_LAND_BASED_RECEIVING:
            land_refs.append(event.kdes["reference_document_number"])

    assert len(land_refs) >= 50
    assert len(land_refs) == len(set(land_refs))
    for value in land_refs:
        _assert_valid_sscc(engine, value)


# ---------------------------------------------------------------------------
# #119 - _advance_time collapsing at the future cap
# ---------------------------------------------------------------------------


def test_timestamps_stay_strictly_increasing_through_a_long_run_at_the_cap():
    engine = LegitFlowEngine(seed=204)
    ceiling_before = datetime.now(UTC) + timedelta(hours=engine_module._max_future_hours())

    timestamps = [engine.next_event()[0].timestamp for _ in range(400)]

    # The per-event advance (10-240 simulated minutes) burns through the
    # ~32h starting headroom in well under 400 calls, so most of this run
    # happens with the cursor pinned at (or past) the cap -- exactly the
    # regime issue #119 reported as collapsing.
    capped_tail = [ts for ts in timestamps if ts >= ceiling_before]
    assert len(capped_tail) > 100

    for earlier, later in zip(timestamps, timestamps[1:]):
        assert later > earlier
    assert len(timestamps) == len(set(timestamps))


def test_consecutive_timestamps_never_collide_once_capped():
    # Mirrors the issue's acceptance criterion directly: no two consecutive
    # generated timestamps are identical once the cursor has reached the
    # live-window ceiling.
    engine = LegitFlowEngine(seed=204)
    previous: datetime | None = None
    saw_sub_minute_pair = False

    for _ in range(400):
        event, _ = engine.next_event()
        current = event.timestamp
        if previous is not None:
            assert current != previous
            if current - previous < timedelta(minutes=1):
                saw_sub_minute_pair = True
        previous = current

    # Confirms the run actually reached the capped regime (successive
    # timestamps far closer together than the 10-240 minute per-event
    # step), so the "never collide" assertion above was meaningfully
    # exercised rather than trivially true of the uncapped phase.
    assert saw_sub_minute_pair


def test_advancing_time_never_exceeds_the_live_window_ceiling_by_more_than_a_tick():
    # The fix rides the ceiling forward, or nudges by a microsecond when the
    # ceiling has not moved -- it must never regress to clamping the cursor
    # backwards, and any overshoot beyond the freshly computed ceiling has
    # to stay within that single tick, not drift by minutes.
    engine = LegitFlowEngine(seed=204)
    for _ in range(400):
        before = engine._time_cursor
        engine._advance_time(30, 180)
        ceiling = datetime.now(UTC) + timedelta(hours=engine_module._max_future_hours())
        assert engine._time_cursor > before
        assert engine._time_cursor <= ceiling + timedelta(microseconds=1)


# ---------------------------------------------------------------------------
# Determinism: neither fix may reorder or add RNG draws
# ---------------------------------------------------------------------------


def _deterministic_fingerprint(engine: LegitFlowEngine, count: int) -> list[tuple]:
    # Deliberately excludes `timestamp` (and any KDE derived from it, like
    # `ship_date`), since those are anchored to wall-clock `now()` by
    # design and are not expected to match bit-for-bit between two engine
    # instances built moments apart. Everything else here is a pure
    # function of the seeded RNG and must match exactly.
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
                tuple(parents),
            )
        )
    return fingerprint


def test_seeded_run_is_reproducible_across_fresh_engines():
    first_run = _deterministic_fingerprint(LegitFlowEngine(seed=204), 250)
    second_run = _deterministic_fingerprint(LegitFlowEngine(seed=204), 250)

    assert first_run == second_run


def test_reset_with_same_seed_reproduces_the_same_run():
    engine = LegitFlowEngine(seed=204)
    first_run = _deterministic_fingerprint(engine, 150)

    engine.reset(seed=204)
    second_run = _deterministic_fingerprint(engine, 150)

    assert first_run == second_run

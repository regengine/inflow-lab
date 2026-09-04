"""Validation-parity tests for MockRegEngineService against live RegEngine
behavior, covering two gaps identified in a repo-wide mock-fidelity audit:

- #101: four checks in the mock's per-event validator --
  traceability_lot_code/product_description length, quantity's positivity,
  and the future-timestamp ceiling -- are actually Pydantic
  Field()/field_validator constraints on RegEngine's real IngestEvent
  model. FastAPI evaluates those while parsing the request body, before
  the route handler runs at all, so live 422s the ENTIRE batch the
  instant any one event trips one of them -- never the "9 accepted, 1
  rejected" HTTP-200 partial success the mock used to return. This file
  pins that MockRegEngineService.ingest() now raises MockRegEngineHTTPError
  (422) for the whole request instead, while the genuinely handler-level
  checks (location, required KDEs, in-batch duplicates) still produce an
  ordinary per-event 200 rejection -- the classification RegEngine itself
  draws between Pydantic validation and route-handler logic.

- #102: RegEngine also rejects any event timestamped more than
  WEBHOOK_MAX_EVENT_AGE_DAYS (default 90) in the past ("replay window
  exceeded"), checked per event inside the route handler -- unlike the
  future ceiling, this is handler-level, not a field constraint, so a
  stale event alone does not 422 the batch; it produces an ordinary
  per-event 200 rejection, same shape as a missing KDE. This file adds
  that floor to the mock (MAX_EVENT_AGE_DAYS,
  validate_event_like_regengine's `now` parameter) and pins it here.

  Every #102 test below drives the check via MockRegEngineService's own
  injectable clock (the same mechanism #120 added for the idempotency TTL,
  constructed directly rather than through the shared app-level
  `controller`) -- never a real sleep, never a bare `datetime.now(UTC)`
  call standing in for "now" -- so the 90-day boundary is exercised
  deterministically regardless of when this suite actually runs.

  MockRegEngineService.__init__ gained a keyword-only
  `enforce_event_age_window` flag (default False) for this. It is off by
  default DELIBERATELY, not an oversight: this repo's own shipped demo
  content (app/demo_fixtures.py's three DemoFixture entries) and a large
  fraction of the existing test suite (tests/test_mock_parity.py,
  tests/test_mock_hmac.py, tests/test_mock_limits.py, tests/test_api.py,
  and others) use a fixed "2026-02-05"-ish timestamp as their canonical
  valid-event fixture. Turning the floor on unconditionally against the
  default wall-clock `clock` would reject that data the instant real time
  drifts more than 90 days past it -- which, as of this file being
  written, it already has. Enforcing it live end-to-end (SimulationController
  constructing MockRegEngineService with the flag on, CSV-import warnings,
  a replay out-of-window count) is simulator-side work outside this file's
  ownership (app/mock_service.py only) -- see the PR/issue notes for the
  exact files and functions that would need it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.mock_service import (
    MAX_EVENT_AGE_DAYS,
    MAX_FUTURE_HOURS,
    MockRegEngineHTTPError,
    MockRegEngineService,
    validate_event_like_regengine,
)
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig
from tests.support.timestamps import recent_event_timestamp


client = TestClient(app)


def setup_function() -> None:
    # Mirrors tests/test_mock_parity.py's own isolation pattern: reset the
    # shared app-global controller/store/mock_service before each test in
    # this module so the HTTP-route tests below don't depend on ordering
    # with other test files. Tests that construct their own
    # MockRegEngineService directly (all of the #102 tests) don't touch
    # this shared state at all.
    asyncio.run(controller.reset(SimulationConfig()))


def _valid_event(
    lot: str = "TLC-PARITY-000001",
    *,
    timestamp: datetime | None = None,
    quantity: float = 100,
    description: str = "Romaine Lettuce",
) -> RegEngineEvent:
    """A minimal HARVESTING event that clears every validation check.

    Mirrors tests/test_mock_parity.py's own helper: HARVESTING has the
    fewest required KDEs (harvest_date, reference_document), which keeps
    this cheap to build. Callers of the _event_with_* factories below
    override exactly one field to make the event fail exactly one check.
    """
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot,
        product_description=description,
        quantity=quantity,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=timestamp or recent_event_timestamp(),
        kdes={
            "harvest_date": "2026-02-05",
            "reference_document": "Harvest Log HAR-0001",
        },
    )


def _event_with_short_lot_code() -> RegEngineEvent:
    return _valid_event(lot="AB")


def _event_with_oversized_description() -> RegEngineEvent:
    return _valid_event(description="X" * 501)


def _event_with_nonpositive_quantity() -> RegEngineEvent:
    return _valid_event(quantity=0)


def _event_with_far_future_timestamp() -> RegEngineEvent:
    # The future ceiling is not clock-injectable (see
    # _field_constraint_errors's docstring in app/mock_service.py for why),
    # so this -- like the pre-existing behavior it is reclassifying, not
    # replacing -- necessarily compares against real wall-clock time. That
    # is unchanged by #101; only its consequence (batch-fatal vs
    # per-event) is new.
    return _valid_event(timestamp=datetime.now(UTC) + timedelta(hours=MAX_FUTURE_HOURS + 1))


def _harvest_event_at(timestamp: datetime, lot: str = "TLC-AGE-000001") -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot,
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=timestamp,
        kdes={
            "harvest_date": timestamp.date().isoformat(),
            "reference_document": "Harvest Log HAR-0001",
        },
    )


# ---------------------------------------------------------------------------
# #101 -- request-fatal field constraints 422 the whole batch, not one event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_event_factory", "expected_message_fragment"),
    [
        (_event_with_short_lot_code, "traceability_lot_code must be at least 3 characters"),
        (_event_with_oversized_description, "product_description must be 1-500 characters"),
        (_event_with_nonpositive_quantity, "quantity must be greater than 0"),
        (
            _event_with_far_future_timestamp,
            f"timestamp is more than {MAX_FUTURE_HOURS} hours in the future",
        ),
    ],
    ids=["short_lot_code", "oversized_description", "nonpositive_quantity", "far_future_timestamp"],
)
def test_field_constraint_violation_rejects_whole_batch_with_422(
    bad_event_factory, expected_message_fragment
) -> None:
    """Each of the four Pydantic-field-constraint checks, alone in an
    otherwise-empty batch, must fail the WHOLE request with 422 -- the
    same outcome shape live RegEngine produces (Acceptance criterion 1 in
    #101: "A CSV import containing one row with a 2-character
    traceability_lot_code produces the same outcome shape in mock mode as
    against live RegEngine.").
    """
    service = MockRegEngineService()
    payload = IngestPayload(source="field-fatal-test", events=[bad_event_factory()])

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload)

    assert exc_info.value.status_code == 422
    assert expected_message_fragment in exc_info.value.detail


def test_field_fatal_violation_in_middle_of_batch_kills_entire_request() -> None:
    """The core #101 regression: before this fix, only the offending event
    was marked "rejected" and the other two were "accepted" in an
    HTTP-200 response -- exactly the "9 accepted, 1 rejected" shape the
    issue describes. Live loses the whole batch; so must the mock, with
    no IngestResponseEvent for anything, good or bad.
    """
    service = MockRegEngineService()
    payload = IngestPayload(
        source="field-fatal-test",
        events=[
            _valid_event(lot="TLC-GOOD-000001"),
            _event_with_nonpositive_quantity(),
            _valid_event(lot="TLC-GOOD-000002"),
        ],
    )

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload)

    assert exc_info.value.status_code == 422
    # Points at the actual offending index within the batch.
    assert "events[1]" in exc_info.value.detail
    assert "quantity must be greater than 0" in exc_info.value.detail


def test_ingest_endpoint_returns_422_not_200_partial_success_for_short_lot_code() -> None:
    """Route-level counterpart: the real HTTP endpoint must surface the
    same 422, not a 200 with a per-event "rejected" entry.
    """
    payload = {
        "source": "test-suite",
        "events": [_event_with_short_lot_code().model_dump(mode="json")],
    }

    response = client.post("/api/mock/regengine/ingest", json=payload)

    assert response.status_code == 422
    assert "traceability_lot_code must be at least 3 characters" in response.json()["detail"]


def test_ingest_endpoint_accepts_valid_batch_unaffected_by_101_change() -> None:
    # Regression guard: the reclassification must not touch events that
    # pass every check.
    payload = {"source": "test-suite", "events": [_valid_event().model_dump(mode="json")]}

    response = client.post("/api/mock/regengine/ingest", json=payload)

    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_missing_kde_is_still_a_per_event_200_rejection_not_a_422() -> None:
    """Contrast case pinning #101's classification (acceptance criterion 3:
    "A test pins which validation checks are batch-fatal versus
    per-event"). Required KDEs are handler-level in live RegEngine, not a
    Pydantic field constraint -- a violation here must still reject only
    the offending event, in an ordinary 200 response, with the rest of the
    batch accepted.
    """
    service = MockRegEngineService()
    good = _valid_event(lot="TLC-GOOD-000001")
    missing_kdes = RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="TLC-BAD-000001",
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=recent_event_timestamp(),
        kdes={},
    )
    payload = IngestPayload(source="field-fatal-test", events=[good, missing_kdes])

    response = service.ingest(payload)  # must NOT raise

    assert response.accepted == 1
    assert response.rejected == 1
    assert response.events[0].status == "accepted"
    assert response.events[1].status == "rejected"
    assert any("Missing required KDEs" in message for message in response.events[1].errors)


def test_validate_event_like_regengine_still_reports_field_constraint_errors_directly() -> None:
    """tests/test_customer_journey.py, tests/test_fixture_quality.py, and
    tests/test_operation_scale.py all call validate_event_like_regengine(event)
    directly and expect the full per-event picture for one event in
    isolation. #101 only changes how MockRegEngineService.ingest()
    classifies these checks at the *batch* level -- this function's own
    contract (report everything wrong with one event) is unchanged.
    """
    errors = validate_event_like_regengine(_event_with_short_lot_code())

    assert "traceability_lot_code must be at least 3 characters" in errors


# ---------------------------------------------------------------------------
# #102 -- 90-day replay-window floor, opt-in, driven by the injectable clock
# ---------------------------------------------------------------------------


def test_age_window_is_enforced_by_default_even_with_an_injected_clock() -> None:
    """The #102 decision, as #209 finally settled it: an unconfigured
    MockRegEngineService() -- what SimulationController's real construction
    uses -- DOES reject an event for being too old.

    This test previously asserted the opposite, and was right to at the time:
    the default was False because flipping it would have rejected this repo's
    own shipped demo fixtures and every test carrying a fixed-date event. Both
    of those are now fixed -- #199 rebases the fixtures onto the current date
    at load time, and tests/support/timestamps.py gives tests an in-window
    canonical timestamp -- so the mock can be faithful to live by default.

    A mock whose default is to skip a check live always applies accepts
    batches that live 422s outright, which is the precise "green demo, failing
    live post" divergence this simulator exists to surface. It was shipping
    inside the simulator itself.

    Uses an injected (not wall-clock) reference point so this holds
    deterministically rather than by coincidence of when the suite runs.
    """
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(clock=lambda: clock_state["now"])
    ancient_event = _harvest_event_at(clock_state["now"] - timedelta(days=1000))
    payload = IngestPayload(source="age-window-test", events=[ancient_event])

    response = service.ingest(payload)

    assert response.accepted == 0
    assert response.rejected == 1
    assert any("replay window exceeded" in error for error in response.events[0].errors)


def test_age_window_can_still_be_opted_out_of() -> None:
    """Opting out stays available and explicit, for a caller whose subject IS
    a stale event or who is knowingly ingesting historical data."""
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"], enforce_event_age_window=False
    )
    ancient_event = _harvest_event_at(clock_state["now"] - timedelta(days=1000))

    response = service.ingest(IngestPayload(source="age-window-test", events=[ancient_event]))

    assert response.accepted == 1
    assert response.rejected == 0


def test_age_window_rejects_event_past_90_days_when_enforced() -> None:
    """Acceptance criterion 1: "validate_event_like_regengine rejects an
    event timestamped more than 90 days in the past, with an error string
    matching RegEngine's wording" -- exercised end-to-end through
    MockRegEngineService.ingest() with enforcement opted in.
    """
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"], enforce_event_age_window=True
    )
    stale_event = _harvest_event_at(
        clock_state["now"] - timedelta(days=MAX_EVENT_AGE_DAYS, seconds=1)
    )
    payload = IngestPayload(source="age-window-test", events=[stale_event])

    response = service.ingest(payload)  # per-event rejection, NOT a 422

    assert response.accepted == 0
    assert response.rejected == 1
    assert response.events[0].status == "rejected"
    (error,) = response.events[0].errors
    assert f"WEBHOOK_MAX_EVENT_AGE_DAYS={MAX_EVENT_AGE_DAYS}" in error
    assert "replay window exceeded" in error


def test_age_window_accepts_event_exactly_at_90_day_boundary() -> None:
    # Symmetric with the future ceiling's own inclusive-boundary
    # convention (exactly MAX_FUTURE_HOURS ahead is accepted): exactly
    # MAX_EVENT_AGE_DAYS old is still inside the window, not outside it.
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"], enforce_event_age_window=True
    )
    boundary_event = _harvest_event_at(clock_state["now"] - timedelta(days=MAX_EVENT_AGE_DAYS))
    payload = IngestPayload(source="age-window-test", events=[boundary_event])

    response = service.ingest(payload)

    assert response.accepted == 1
    assert response.rejected == 0


def test_age_window_rejection_is_per_event_not_batch_fatal() -> None:
    """Contrast with #101: unlike a field constraint, RegEngine's replay-
    window floor is checked inside the route handler, per event -- a stale
    timestamp rejects only that one event. The rest of the batch still
    succeeds with an ordinary 200, not a 422 for everything.
    """
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"], enforce_event_age_window=True
    )
    fresh_event = _harvest_event_at(clock_state["now"] - timedelta(days=1), lot="TLC-AGE-FRESH")
    stale_event = _harvest_event_at(
        clock_state["now"] - timedelta(days=MAX_EVENT_AGE_DAYS + 5), lot="TLC-AGE-STALE"
    )
    payload = IngestPayload(source="age-window-test", events=[fresh_event, stale_event])

    response = service.ingest(payload)  # must NOT raise

    assert response.accepted == 1
    assert response.rejected == 1
    assert response.events[0].status == "accepted"
    assert response.events[1].status == "rejected"


def test_age_window_is_driven_by_injected_clock_not_wall_clock_and_not_sleep() -> None:
    """Advancing the injected clock -- never sleeping, never depending on
    real wall-clock time -- moves the SAME fixed event timestamp across
    the 90-day boundary, proving the check is driven by
    MockRegEngineService's injectable clock (#120's mechanism), exactly as
    instructed for #102.
    """
    fixed_timestamp = datetime(2026, 3, 1, tzinfo=UTC)
    clock_state = {"now": fixed_timestamp + timedelta(days=MAX_EVENT_AGE_DAYS, seconds=1)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"], enforce_event_age_window=True
    )

    just_past_window = service.ingest(
        IngestPayload(source="age-window-test", events=[_harvest_event_at(fixed_timestamp)])
    )
    assert just_past_window.rejected == 1

    # Wind the clock back to just before the event ages past the window --
    # same event timestamp, same service instance, only the clock moved.
    clock_state["now"] = fixed_timestamp + timedelta(days=MAX_EVENT_AGE_DAYS)
    now_within_window = service.ingest(
        IngestPayload(
            source="age-window-test",
            events=[_harvest_event_at(fixed_timestamp, lot="TLC-AGE-000002")],
        )
    )
    assert now_within_window.accepted == 1


def test_validate_event_like_regengine_only_flags_age_when_now_is_provided() -> None:
    """Direct-call contract for the `now` parameter: omitted (the default
    every pre-existing caller uses) skips the age check entirely;
    supplied, it applies it. Both calls examine the exact same stale
    event.
    """
    reference = datetime(2026, 6, 1, tzinfo=UTC)
    stale_event = _harvest_event_at(reference - timedelta(days=MAX_EVENT_AGE_DAYS + 1))

    assert validate_event_like_regengine(stale_event) == []

    errors_with_now = validate_event_like_regengine(stale_event, now=reference)
    assert len(errors_with_now) == 1
    assert "replay window exceeded" in errors_with_now[0]


def test_pins_90_day_age_floor_and_24h_future_ceiling_against_regengine_defaults() -> None:
    """Acceptance criterion 3: "A test pins the 90-day floor and the
    24-hour ceiling against RegEngine's WEBHOOK_MAX_EVENT_AGE_DAYS /
    WEBHOOK_MAX_EVENT_FUTURE_HOURS defaults." This file owns only the
    mock's half of that pin (app/mock_service.py, per this task's file
    ownership) -- tests/test_regengine_contract_pin.py and app/contract.py
    are separate files this task does not touch.
    """
    assert MAX_EVENT_AGE_DAYS == 90
    assert MAX_FUTURE_HOURS == 24


def test_configurable_max_event_age_days_shortens_replay_window() -> None:
    """#217: max_event_age_days lets an operator tighten the replay window.

    A service configured with max_event_age_days=7 should reject an event
    that is 8 days old but accept one that is 6 days old, while the
    default 90-day service accepts both.
    """
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    short_window = MockRegEngineService(
        clock=lambda: clock_state["now"],
        enforce_event_age_window=True,
        max_event_age_days=7,
    )
    default_window = MockRegEngineService(
        clock=lambda: clock_state["now"],
        enforce_event_age_window=True,
    )

    eight_days_old = _harvest_event_at(clock_state["now"] - timedelta(days=8), lot="TLC-AGE-8D")
    six_days_old = _harvest_event_at(clock_state["now"] - timedelta(days=6), lot="TLC-AGE-6D")

    # Short window rejects the 8-day-old event...
    resp_short_8 = short_window.ingest(IngestPayload(source="cfg-age", events=[eight_days_old]))
    assert resp_short_8.rejected == 1
    assert "replay window exceeded" in resp_short_8.events[0].errors[0]

    # ...but accepts the 6-day-old one.
    resp_short_6 = short_window.ingest(IngestPayload(source="cfg-age", events=[six_days_old]))
    assert resp_short_6.accepted == 1

    # Default 90-day window accepts both.
    resp_default_8 = default_window.ingest(
        IngestPayload(source="cfg-age", events=[_harvest_event_at(clock_state["now"] - timedelta(days=8), lot="TLC-AGE-8D-2")])
    )
    assert resp_default_8.accepted == 1


def test_bypass_window_counts_out_of_window_events_without_rejecting() -> None:
    """#217: when enforce_event_age_window=False, events outside the window
    are accepted but counted in out_of_window_events for visibility."""
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"],
        enforce_event_age_window=False,
    )
    stale_event = _harvest_event_at(
        clock_state["now"] - timedelta(days=MAX_EVENT_AGE_DAYS + 5), lot="TLC-BYPASS-STALE"
    )
    fresh_event = _harvest_event_at(clock_state["now"] - timedelta(days=1), lot="TLC-BYPASS-FRESH")
    payload = IngestPayload(source="bypass-test", events=[stale_event, fresh_event])

    response = service.ingest(payload)

    assert response.accepted == 2
    assert response.rejected == 0
    assert response.out_of_window_events == 1
    assert response.event_age_window_mode == "bypassed"
    assert response.event_age_window_days == MAX_EVENT_AGE_DAYS


def test_enforced_window_sets_mode_and_zero_bypass_count() -> None:
    """#217: when enforced, out_of_window_events is 0 and mode is 'enforced'."""
    clock_state = {"now": datetime(2026, 6, 1, tzinfo=UTC)}
    service = MockRegEngineService(
        clock=lambda: clock_state["now"],
        enforce_event_age_window=True,
    )
    stale_event = _harvest_event_at(
        clock_state["now"] - timedelta(days=MAX_EVENT_AGE_DAYS + 5), lot="TLC-ENF-STALE"
    )
    fresh_event = _harvest_event_at(clock_state["now"] - timedelta(days=1), lot="TLC-ENF-FRESH")
    payload = IngestPayload(source="enforced-test", events=[stale_event, fresh_event])

    response = service.ingest(payload)

    assert response.accepted == 1
    assert response.rejected == 1
    assert response.out_of_window_events == 0
    assert response.event_age_window_mode == "enforced"
    assert response.event_age_window_days == MAX_EVENT_AGE_DAYS

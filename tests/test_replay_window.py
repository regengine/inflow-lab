"""#102 -- the 90-day event-age replay window, at the input boundary.

RegEngine rejects any event older than WEBHOOK_MAX_EVENT_AGE_DAYS (90)
with "replay window exceeded". The mock now models that floor
(app/mock_service.py's MAX_EVENT_AGE_DAYS, pinned in
tests/test_mock_validation_parity.py), but it ships OFF by default and
this file is where that decision is pinned and paid for.

Why it stays off: turning it on rejects this repository's own bundled
demo fixtures, which carry hardcoded dates already more than 90 days
stale, plus the fixed "2026-02-05" timestamp a dozen-plus test modules
use as their canonical valid event. Load Demo Fixture -- the flow the
fixtures exist to make reliable -- would break on the repo's own data.
That is tracked as #199, and re-timing the fixtures is its work, not
this file's.

What that leaves is #102's acceptance criterion 2, which is not blocked
by any of it: warn at the input boundary, before delivery. A design
partner backfilling last season's harvest records gets told the rows are
outside live's window while they are still in mock mode, instead of
discovering it when the whole batch comes back rejected from production.
A warning, not a rejection -- importing historical data into a simulator
is legitimate, and the mock genuinely does accept it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import tenancy
from app.csv_importer import parse_csv_import
from app.main import app, controller
from app.mock_service import MAX_EVENT_AGE_DAYS
from app.schemas.domain import CSVImportType
from app.schemas.simulation import SimulationConfig


client = TestClient(app)

SCHEDULED_HEADER = (
    "cte_type,traceability_lot_code,product_description,quantity,"
    "unit_of_measure,location_name,timestamp,harvest_date,reference_document"
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def _csv_at(timestamp: datetime, lot: str = "TLC-AGE-000001") -> str:
    stamp = timestamp.isoformat().replace("+00:00", "Z")
    return (
        f"{SCHEDULED_HEADER}\n"
        f"harvesting,{lot},Romaine Lettuce,100,cases,Valley Fresh Farms,"
        f"{stamp},{timestamp.date().isoformat()},Harvest Log HL-AGE-001\n"
    )


def _replay_warnings(parsed) -> list:
    return [w for w in parsed.warnings if "replay window" in w.message]


def test_a_row_older_than_the_window_is_warned_about_before_delivery() -> None:
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        _csv_at(_NOW - timedelta(days=MAX_EVENT_AGE_DAYS + 30)),
        default_timestamp=_NOW,
    )

    assert parsed.errors == [], "a historical import must still be importable"
    assert len(parsed.events) == 1, "the row is warned about, not dropped"

    warnings = _replay_warnings(parsed)
    assert len(warnings) == 1, "a stale row should surface exactly one replay-window warning"
    warning = warnings[0]
    assert warning.row == 2
    assert warning.field == "timestamp"
    assert warning.severity == "required", (
        "unlike a missing recommended KDE, live WILL reject this row"
    )
    assert str(MAX_EVENT_AGE_DAYS) in warning.message
    assert "120 days old" in warning.message


def test_a_row_inside_the_window_is_not_warned_about() -> None:
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        _csv_at(_NOW - timedelta(days=MAX_EVENT_AGE_DAYS - 1)),
        default_timestamp=_NOW,
    )

    assert _replay_warnings(parsed) == []


def test_the_window_boundary_matches_the_mock_service_exactly() -> None:
    """Exactly MAX_EVENT_AGE_DAYS old is inside the window, not outside it
    -- the same inclusive convention _handler_level_errors uses. An importer
    that warned here while the mock accepted would be a second, independent
    definition of the same limit, which is the drift this repo exists to
    catch rather than introduce."""
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        _csv_at(_NOW - timedelta(days=MAX_EVENT_AGE_DAYS)),
        default_timestamp=_NOW,
    )
    assert _replay_warnings(parsed) == []

    just_past = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        _csv_at(_NOW - timedelta(days=MAX_EVENT_AGE_DAYS, seconds=1)),
        default_timestamp=_NOW,
    )
    assert len(_replay_warnings(just_past)) == 1


def test_the_warning_reaches_the_api_caller_alongside_a_successful_mock_delivery() -> None:
    """The whole point is that this arrives while the import still
    succeeds. In mock mode the batch is accepted, hashed and chained -- the
    warning is the only thing standing between that green result and a
    wholesale rejection against live."""
    stale = datetime.now(UTC) - timedelta(days=MAX_EVENT_AGE_DAYS + 10)

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "scheduled_events",
            "csv_text": _csv_at(stale, lot="TLC-AGE-ROUTE-1"),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["posted"] == 1, "mock delivery still succeeds; that is the divergence"
    replay_warnings = [w for w in body["warnings"] if "replay window" in w["message"]]
    assert len(replay_warnings) == 1
    assert replay_warnings[0]["severity"] == "required"


def test_seed_lot_rows_without_a_timestamp_are_never_stale() -> None:
    # Seed lots default to import time, so they cannot fall outside the
    # window -- the warning must not fire on them.
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
        "TLC-SEED-AGE,Romaine Hearts,42,cases,Valley Fresh Farms\n",
        default_timestamp=_NOW,
    )

    assert _replay_warnings(parsed) == []


@pytest.mark.parametrize("service", [tenancy.mock_service])
def test_the_mock_still_does_not_enforce_the_window_by_default(service) -> None:
    """Pins the deliberate scoping decision so a future flip is a choice
    rather than an accident.

    Enforcing the floor in the deployed mock requires #199 first: the
    shipped demo fixtures carry hardcoded dates already outside the window,
    so switching this on rejects Load Demo Fixture on the repo's own data.
    When #199 re-times the fixtures (and the suite's canonical
    "2026-02-05" event), this assertion is the one to invert -- not to
    delete.
    """
    assert service._enforce_event_age_window is False

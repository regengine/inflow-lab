"""#102/#209 -- the 90-day event-age replay window, end to end.

RegEngine rejects any event older than WEBHOOK_MAX_EVENT_AGE_DAYS (90)
with "replay window exceeded", per event, inside the route handler. The
mock models that floor (app/mock_service.py's MAX_EVENT_AGE_DAYS, pinned
in tests/test_mock_validation_parity.py) and, since #209, enforces it by
default -- including in the deployed stand-in real callers post at. This
file is where that is pinned at both ends: the warning the importer emits
before delivery, and the rejection delivery itself now produces.

The two are different jobs and both are wanted:

* The CSV importer warns on every row outside the window at parse time, at
  `required` severity. That reaches a design partner backfilling last
  season's harvest records while they are still choosing what to import,
  naming the limit and live's exact wording.
* The mock then rejects those rows on delivery, exactly as live would.
  Before #209 it accepted them, which made the warning the only thing
  standing between a green demo and a wholesale rejection in production.
  It is no longer load-bearing in that way -- it is now advance notice of
  a refusal the simulator itself will issue.

Scope, stated precisely: an event-age floor BOUNDS replay, it does not
close it. It caps how long a captured signed request stays replayable at
the window's width; it does nothing about a replay of a fresh capture,
which is a per-request signed-timestamp-plus-nonce problem that neither
this repo nor RegEngine solves today. RegEngine's own
_validate_event_timestamp_window says as much in its docstring
("Separate from signature-level replay defense: this catches captured
webhooks replayed months later with a freshly-computed signature").
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


def _harvest_event_payload(timestamp: datetime, lot: str) -> dict:
    """A wire-shaped HARVESTING event that clears every check except,
    possibly, the age window -- so a rejection can only be about the age.
    """
    stamp = timestamp.isoformat().replace("+00:00", "Z")
    return {
        "cte_type": "harvesting",
        "traceability_lot_code": lot,
        "product_description": "Romaine Lettuce",
        "quantity": 100,
        "unit_of_measure": "cases",
        "location_name": "Valley Fresh Farms",
        "timestamp": stamp,
        "kdes": {
            "harvest_date": timestamp.date().isoformat(),
            "reference_document": f"Harvest Log {lot}",
        },
    }


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


def test_the_warning_and_the_delivery_rejection_now_agree_on_a_stale_row() -> None:
    """The divergence #209 closed, pinned from the API caller's side.

    Before the default flip this test asserted ``posted == 1``: the
    importer warned that live would refuse the row, and then the mock
    accepted it anyway. That gap is the entire failure mode this repository
    exists to surface -- a historical CSV import that goes green in the
    demo and 422s on the first real post -- and it was shipping inside the
    simulator itself.

    Now both halves agree. The row still *imports* (accepted and stored --
    pulling historical data into a simulator is legitimate and is not
    silently dropped), the warning still arrives at parse time, and
    delivery reports the same refusal live would, in live's own wording,
    per event rather than 422ing the batch.
    """
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
    assert body["accepted"] == 1, "a historical row must still import"
    assert body["stored"] == 1, "and still be persisted, not silently dropped"
    assert body["posted"] == 0, "mock delivery now refuses it, exactly as live does"
    assert body["failed"] == 1

    # The warning is still emitted -- it is now advance notice of a refusal
    # the simulator itself will issue, rather than the only signal.
    replay_warnings = [w for w in body["warnings"] if "replay window" in w["message"]]
    assert len(replay_warnings) == 1
    assert replay_warnings[0]["severity"] == "required"

    stored = client.get("/api/events?limit=5").json()["events"][0]
    assert stored["delivery_status"] == "failed"
    assert "replay window exceeded" in stored["error"]


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
def test_the_deployed_mock_now_enforces_the_window_by_default(service) -> None:
    """The inverted pin (#209). Its predecessor asserted ``is False`` and
    said in its own docstring that when #199 re-timed the fixtures and the
    suite's canonical event, "this assertion is the one to invert -- not to
    delete". Both preconditions are met, so it is inverted here.

    This is the default-tenant service that ``app.main`` actually serves --
    the object a request to /api/mock/regengine/ingest reaches. #209's
    finding was precisely that the flag existed while no production caller
    took it; asserting on the deployed instance, rather than on a service
    this test builds itself, is what makes that finding un-reintroducible.
    """
    assert service._enforce_event_age_window is True


def test_a_freshly_created_tenant_controller_enforces_the_window_too() -> None:
    """The second construction site (#209).

    ``app/tenancy.py`` mints a fresh SimulationController -- with its own
    MockRegEngineService -- the first time a request names an unknown
    tenant id. Pinning only the default tenant's service would leave that
    path free to drift back to accepting what live rejects, and a tenant
    demo is exactly as customer-facing as the default one.
    """
    tenant_id = "replay-window-probe"
    try:
        tenant_controller = tenancy.get_tenant_controller_for_id(tenant_id)
        assert tenant_controller.mock_service._enforce_event_age_window is True
    finally:
        tenancy.pop_tenant_controller(tenant_id)
        tenancy.finish_tenant_delete(tenant_id)


def test_a_stale_event_is_rejected_through_the_production_ingest_route() -> None:
    """End-to-end proof, through the route rather than the flag.

    Everything above inspects state; this posts a batch at the real
    endpoint, on the real app, with no hand-constructed service anywhere,
    and shows the stale event refused and the fresh one accepted in the
    same request. That per-event split is live's own shape: the replay
    window is checked inside RegEngine's route handler, so a stale
    timestamp costs you that event, not the batch.
    """
    stale = datetime.now(UTC) - timedelta(days=MAX_EVENT_AGE_DAYS + 1)
    fresh = datetime.now(UTC) - timedelta(hours=12)

    response = client.post(
        "/api/mock/regengine/ingest",
        json={
            "source": "replay-window-e2e",
            "events": [
                _harvest_event_payload(stale, "TLC-AGE-E2E-STALE"),
                _harvest_event_payload(fresh, "TLC-AGE-E2E-FRESH"),
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["accepted"], body["rejected"]) == (1, 1)

    stale_result, fresh_result = body["events"]
    assert stale_result["status"] == "rejected"
    assert stale_result["errors"] == [
        f"timestamp is older than WEBHOOK_MAX_EVENT_AGE_DAYS={MAX_EVENT_AGE_DAYS} "
        "— replay window exceeded"
    ]
    assert stale_result["chain_hash"] is None, "a refused event must not extend the chain"
    assert fresh_result["status"] == "accepted"
    assert fresh_result["chain_hash"]


def test_the_window_bounds_replay_rather_than_closing_it() -> None:
    """States the limit of this defence as an executable claim, so nobody
    reads #209 as "replay is handled".

    Replaying a capture whose events are still inside the window succeeds
    every time, byte-identical, with no Idempotency-Key to dedupe it. What
    the floor removes is the unbounded tail -- the capture stops working
    once its events age out -- not replay itself. Closing that needs a
    signed timestamp plus a nonce on the request, which neither this
    simulator nor RegEngine has today.
    """
    captured = {
        "source": "replay-window-capture",
        "events": [
            _harvest_event_payload(datetime.now(UTC) - timedelta(hours=12), "TLC-AGE-REPLAYABLE")
        ],
    }

    statuses = []
    for _ in range(6):
        replayed = client.post("/api/mock/regengine/ingest", json=captured)
        assert replayed.status_code == 200, replayed.text
        statuses.append(replayed.json()["events"][0]["status"])

    assert statuses == ["accepted"] * 6, (
        "an event-age window does not stop replay of a fresh capture -- "
        "that needs a per-request nonce, which is not what #209 delivers"
    )

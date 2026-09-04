"""Coverage for SimulationController.retry_failed_delivery's least-common
response shapes (#163): the DestinationMode.NONE "skipped" branch, the
mixed-outcome "partial" status, and record_ids/limit narrowing a retry to
specific records -- including an unknown record_id.

Every retry test that already existed before this file retried the full,
unscoped failed set in mock or live mode (tests/test_api.py), or scoped to
a subset made entirely of record_ids that do exist
(tests/test_api_robustness.py's
test_retry_with_nonempty_record_ids_still_filters_to_that_subset). None of
those set delivery mode to "none" for a retry, produced mixed per-record
outcomes in one batch, passed an unknown record_id, or asserted the
`skipped` field's value at all. This file is deliberately narrow to just
those still-uncovered shapes rather than re-covering the full-batch or
known-subset cases those other files already establish.

Kept in its own file rather than added to tests/test_api.py or
tests/test_api_robustness.py -- neither may be edited (strict per-file
ownership across parallel workstreams).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.simulation import SimulationConfig
from tests.support.timestamps import recent_event_timestamp


client = TestClient(app)

_BASE_TIME = recent_event_timestamp()
_VALID_KDES = {"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-RETRY-DEFAULT"}


def setup_function() -> None:
    # app/main.py's `controller` is a module-level singleton shared by every
    # test file in the process (same convention as tests/test_api.py and
    # tests/test_api_robustness.py's own setup_function) -- reset it before
    # each test so records left behind by another test can't leak in.
    asyncio.run(controller.reset(SimulationConfig()))


def _make_failed_record(
    lot: str,
    *,
    minutes: int = 0,
    kdes: dict[str, Any] | None = None,
    payload_source: str = "retry-paths-suite",
) -> StoredEventRecord:
    """Build a record straight into the store as already-failed, bypassing
    the simulator loop -- as if a prior delivery attempt (live or mock)
    failed and the operator is about to retry it.

    Mirrors tests/test_api_robustness.py's own _make_stored_record helper.
    Kept local instead of imported: test files are independent of each
    other, and this file owns nothing in that one.

    `kdes` defaults to a complete, valid set so the record retries cleanly
    in mock mode; passing `kdes={}` (missing the required harvest_date and
    reference_document) is how the "partial" test below makes one specific
    record fail mock validation on retry while its sibling succeeds.
    """
    return StoredEventRecord(
        payload_source=payload_source,
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=_BASE_TIME + timedelta(minutes=minutes),
            kdes=_VALID_KDES if kdes is None else kdes,
        ),
        destination_mode=DestinationMode.NONE,
        delivery_status="failed",
        error="temporary outage",
    )


# ---------------------------------------------------------------------------
# #163 -- delivery.mode == NONE at retry time: "skipped", not attempted
# ---------------------------------------------------------------------------


def test_retry_with_delivery_mode_none_returns_skipped_status() -> None:
    failed = _make_failed_record("TLC-RETRY-SKIP-001")
    controller.store.add_many([failed])

    response = client.post("/api/delivery/retry", json={"delivery": {"mode": "none"}})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["requested"] == 1
    assert body["retryable"] == 1
    assert body["attempted"] == 0
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["skipped"] == 1
    assert body["delivery_mode"] == "none"
    assert body["record_ids"] == [failed.record_id]
    assert body["error"] == "Retry requires mock or live delivery mode."

    # Nothing was actually attempted -- the record must be untouched, not
    # merely reported as untouched.
    events = client.get("/api/events?limit=10").json()["events"]
    record = next(event for event in events if event["record_id"] == failed.record_id)
    assert record["delivery_status"] == "failed"
    assert record["delivery_attempts"] == 0


# ---------------------------------------------------------------------------
# #163 -- one retry batch with both a recoverable and a still-failing
# record: "partial", not "posted" or "failed"
# ---------------------------------------------------------------------------


def test_retry_batch_with_mixed_per_record_outcomes_returns_partial_status() -> None:
    # Same payload_source and no stored idempotency_key on either record --
    # both group into a single mock_service.ingest() call (see
    # SimulationController.retry_failed_delivery's grouped_records), so
    # this is genuinely one batch producing a mixed outcome, not two
    # separate all-or-nothing calls that happen to sum to a mix.
    recoverable = _make_failed_record("TLC-RETRY-PARTIAL-GOOD", minutes=0)
    # Empty kdes is missing HARVESTING's required harvest_date and
    # reference_document, so the mock rejects this specific event on
    # retry while its batch-mate above is accepted.
    still_broken = _make_failed_record("TLC-RETRY-PARTIAL-BAD", minutes=1, kdes={})
    controller.store.add_many([recoverable, still_broken])

    response = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["requested"] == 2
    assert body["retryable"] == 2
    assert body["attempted"] == 2
    assert body["posted"] == 1
    assert body["failed"] == 1
    assert body["skipped"] == 0
    assert body["delivery_mode"] == "mock"
    assert set(body["record_ids"]) == {recoverable.record_id, still_broken.record_id}
    # The per-event rejection lives in that event's own record, not the
    # batch-level `error` -- the mock call itself succeeded (HTTP-level),
    # it just accepted one event and rejected the other.
    assert body["error"] is None

    events = client.get("/api/events?limit=10").json()["events"]
    by_id = {event["record_id"]: event for event in events}

    recovered_record = by_id[recoverable.record_id]
    assert recovered_record["delivery_status"] == "posted"
    assert recovered_record["delivery_attempts"] == 1
    assert recovered_record["error"] is None
    assert recovered_record["last_delivery_success_at"]

    still_failed_record = by_id[still_broken.record_id]
    assert still_failed_record["delivery_status"] == "failed"
    assert still_failed_record["delivery_attempts"] == 1
    assert still_failed_record["error"] == (
        "Missing required KDEs for harvesting: harvest_date, reference_document"
    )
    assert still_failed_record["last_delivery_success_at"] is None


# ---------------------------------------------------------------------------
# #163 -- an unknown record_id must yield nonzero `skipped`, not an error
# ---------------------------------------------------------------------------


def test_retry_with_unknown_record_id_returns_nonzero_skipped_without_erroring() -> None:
    target = _make_failed_record("TLC-RETRY-UNKNOWN-TARGET")
    other = _make_failed_record("TLC-RETRY-UNKNOWN-OTHER", minutes=1)
    controller.store.add_many([target, other])

    response = client.post(
        "/api/delivery/retry",
        json={
            "record_ids": [target.record_id, "missing-record-id-does-not-exist"],
            "delivery": {"mode": "mock"},
        },
    )

    # The unknown id must not raise or 4xx/5xx -- it is simply not found.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["requested"] == 2
    assert body["retryable"] == 1
    assert body["attempted"] == 1
    assert body["posted"] == 1
    assert body["skipped"] == 1
    # Only the real record is ever touched -- the unknown id contributes to
    # `skipped`, never to `record_ids`.
    assert body["record_ids"] == [target.record_id]


# ---------------------------------------------------------------------------
# #163 -- `limit` narrows how many failed records a retry attempts
# ---------------------------------------------------------------------------


def test_retry_limit_narrows_which_failed_records_are_attempted() -> None:
    records = [
        _make_failed_record(f"TLC-RETRY-LIMIT-{index}", minutes=index) for index in range(3)
    ]
    controller.store.add_many(records)

    response = client.post("/api/delivery/retry", json={"limit": 1, "delivery": {"mode": "mock"}})

    assert response.status_code == 200
    body = response.json()
    assert body["retryable"] == 1
    assert body["attempted"] == 1
    assert len(body["record_ids"]) == 1

    # Only one of the three failed records was ever attempted -- the other
    # two must still be sitting there failed, exactly as before this call.
    events = client.get("/api/events?limit=10").json()["events"]
    still_failed = [event for event in events if event["delivery_status"] == "failed"]
    assert len(still_failed) == 2
    assert all(event["delivery_attempts"] == 0 for event in still_failed)

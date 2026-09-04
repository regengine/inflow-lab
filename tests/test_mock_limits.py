"""Coverage for MockRegEngineService.ingest's in-batch dedup and idempotency
cache eviction (#123).

app/mock_service.py has three validation/bookkeeping branches an audit
found completely uncovered: the >500-events batch cap, in-batch duplicate
detection (same CTE + lot + timestamp + location), and the 1024-entry
idempotency-cache LRU eviction. The batch cap is deliberately NOT
re-tested here -- tests/test_mock_parity.py (#142) already covers it
thoroughly, including the exact >500/==500 boundary and that it surfaces
as 422 through the real HTTP route rather than an unhandled 500. This file
covers the other two: in-batch duplicate rejection and cache eviction.
"""

from __future__ import annotations

from datetime import datetime

from app.mock_service import _IDEMPOTENCY_CACHE_LIMIT, MockRegEngineService
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from tests.support.timestamps import recent_event_timestamp


def _valid_event(
    lot: str = "TLC-LIMITS-000001",
    *,
    timestamp: datetime | None = None,
    location: str = "Valley Fresh Farms",
) -> RegEngineEvent:
    """A minimal HARVESTING event that clears every validate_event_like_regengine check.

    Mirrors tests/test_mock_parity.py's own helper: HARVESTING has the
    fewest required KDEs (harvest_date, reference_document), which keeps
    this cheap to build even called many times over (the eviction test
    below calls it 1000+ times).
    """
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot,
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name=location,
        timestamp=timestamp or recent_event_timestamp(),
        kdes={
            "harvest_date": "2026-02-05",
            "reference_document": "Harvest Log HAR-0001",
        },
    )


# ---------------------------------------------------------------------------
# #123 -- in-batch duplicate detection (same CTE, lot, timestamp, location)
# ---------------------------------------------------------------------------


def test_duplicate_event_within_batch_is_rejected_once() -> None:
    service = MockRegEngineService()
    event = _valid_event()
    payload = IngestPayload(source="dup-test", events=[event, event])

    response = service.ingest(payload)

    # The first occurrence is a normal accept -- only the second, once its
    # key has already been seen this batch, is flagged as a duplicate.
    assert response.accepted == 1
    assert response.rejected == 1
    assert response.total == 2
    assert response.events[0].status == "accepted"
    assert response.events[1].status == "rejected"
    assert response.events[1].errors == [
        "Duplicate event in batch (same CTE, lot, timestamp, and location)"
    ]


def test_events_sharing_cte_lot_and_timestamp_but_different_location_are_not_duplicates() -> None:
    # The dedup key is CTE + lot + timestamp + location, all four -- two
    # events that agree on the first three but ship from different
    # locations describe two distinct real-world events, not a duplicate.
    # This guards against a regression that drops location from the key.
    service = MockRegEngineService()
    payload = IngestPayload(
        source="dup-test",
        events=[
            _valid_event(location="Valley Fresh Farms"),
            _valid_event(location="Coastal Packhouse"),
        ],
    )

    response = service.ingest(payload)

    assert response.accepted == 2
    assert response.rejected == 0
    assert [event.status for event in response.events] == ["accepted", "accepted"]


# ---------------------------------------------------------------------------
# #123 -- idempotency cache LRU eviction past 1024 entries
# ---------------------------------------------------------------------------


def test_idempotency_cache_evicts_oldest_entry_past_1024_limit() -> None:
    service = MockRegEngineService()
    total_keys = _IDEMPOTENCY_CACHE_LIMIT + 1
    first_response = None
    last_response = None

    # Insert one more distinct idempotency key than the cache can hold, in
    # order, with no repeats -- a pure FIFO fill that must push the very
    # first entry out once the 1025th is inserted.
    for index in range(total_keys):
        payload = IngestPayload(
            source="evict-test", events=[_valid_event(lot=f"TLC-EVICT-{index:05d}")]
        )
        response = service.ingest(payload, idempotency_key=f"evict-key-{index}")
        if index == 0:
            first_response = response
        last_response = response

    # The oldest key was evicted: replaying it must ingest as a brand new
    # event (a new event_id) rather than returning the stale cached
    # response -- that is what "no longer replays" means operationally.
    replayed_oldest = service.ingest(
        IngestPayload(source="evict-test", events=[_valid_event(lot="TLC-EVICT-REPLAY-1")]),
        idempotency_key="evict-key-0",
    )
    assert replayed_oldest.events[0].event_id != first_response.events[0].event_id

    # The most-recently-inserted key is still well within the cap and must
    # still replay its exact original response -- even against a totally
    # different payload -- which is what distinguishes a real cache hit
    # from this assertion passing by coincidence.
    last_key = f"evict-key-{total_keys - 1}"
    replayed_newest = service.ingest(
        IngestPayload(source="evict-test", events=[_valid_event(lot="TLC-EVICT-REPLAY-2")]),
        idempotency_key=last_key,
    )
    assert replayed_newest.events[0].event_id == last_response.events[0].event_id

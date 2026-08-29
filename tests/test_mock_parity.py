"""Parity tests for MockRegEngineService against live RegEngine behavior.

Covers four gaps identified in a repo-wide mock-fidelity audit:

- #118: an empty ``events`` batch must be rejected (422), not accepted as a
  fake no-op success.
- #142: a batch over the 500-event cap must surface as a 422 through the
  real HTTP route, not an unhandled 500.
- #120: the idempotency cache must expire replays after 24h, not replay (or
  evict) purely on entry count.
- #122: a fresh ``MockRegEngineService`` must resume ``chain_hash`` from a
  tenant's persisted event log instead of forking a new lineage from "".

Tests that only need the service in isolation construct their own
``MockRegEngineService`` directly. Tests for #118 and #142 that specifically
depend on the HTTP route's exception handling go through the real FastAPI
app via ``TestClient``, since that is the only way to observe a status code.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.mock_service import (
    IDEMPOTENCY_TTL_HOURS,
    MAX_BATCH_EVENTS,
    MIN_BATCH_EVENTS,
    MockRegEngineHTTPError,
    MockRegEngineService,
)
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig
from app.store import EventStore
from tests.support.timestamps import CANONICAL_EVENT_DATE, CANONICAL_EVENT_TIME


client = TestClient(app)


def setup_function() -> None:
    # Mirrors tests/test_api.py's own isolation pattern: reset the shared
    # app-global controller/store/mock_service before each test in this
    # module so the HTTP-level tests below don't depend on ordering with
    # other test files.
    asyncio.run(controller.reset(SimulationConfig()))


def _valid_event(lot: str = "TLC-PARITY-000001", *, timestamp: datetime | None = None) -> RegEngineEvent:
    """A minimal HARVESTING event that clears every validate_event_like_regengine check.

    HARVESTING has the fewest required KDEs (harvest_date, reference_document),
    which keeps this helper -- and the batch-size tests that reuse it many
    times over -- cheap to build.
    """
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot,
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=timestamp or CANONICAL_EVENT_TIME,
        kdes={
            "harvest_date": CANONICAL_EVENT_DATE,
            "reference_document": "Harvest Log HAR-0001",
        },
    )


# --- #118: empty batches must be rejected like an out-of-range batch -----


def test_mock_service_rejects_empty_batch_like_regengine() -> None:
    service = MockRegEngineService()
    payload = IngestPayload(source="unit-test", events=[])

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload)

    assert exc_info.value.status_code == 422
    assert str(MIN_BATCH_EVENTS) in exc_info.value.detail


def test_ingest_endpoint_rejects_empty_batch_with_422() -> None:
    response = client.post("/api/mock/regengine/ingest", json={"source": "test-suite", "events": []})

    assert response.status_code == 422
    assert str(MIN_BATCH_EVENTS) in response.json()["detail"]


def test_ingest_endpoint_accepts_single_event_batch() -> None:
    # The MIN_BATCH_EVENTS boundary is inclusive -- 1 event is a normal batch.
    payload = {"source": "test-suite", "events": [_valid_event().model_dump(mode="json")]}

    response = client.post("/api/mock/regengine/ingest", json=payload)

    assert response.status_code == 200
    assert response.json()["accepted"] == 1


# --- #142: a >500 batch must surface as 422 through the HTTP route, not 500 --


def test_mock_service_rejects_batch_over_max_events() -> None:
    service = MockRegEngineService()
    # Batch-size is checked before any per-event validation, so a single
    # repeated (even otherwise-identical) event is enough to exercise it.
    payload = IngestPayload(source="unit-test", events=[_valid_event()] * (MAX_BATCH_EVENTS + 1))

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload)

    assert exc_info.value.status_code == 422
    assert str(MAX_BATCH_EVENTS) in exc_info.value.detail


def test_ingest_endpoint_returns_422_not_500_for_batch_over_max_events() -> None:
    event_dict = _valid_event().model_dump(mode="json")
    payload = {"source": "test-suite", "events": [event_dict] * (MAX_BATCH_EVENTS + 1)}

    response = client.post("/api/mock/regengine/ingest", json=payload)

    # Before #142's fix, MockRegEngineHTTPError propagated past the route
    # uncaught and FastAPI's default handling turned it into a 500.
    assert response.status_code == 422
    assert response.status_code != 500
    assert str(MAX_BATCH_EVENTS) in response.json()["detail"]


def test_ingest_endpoint_accepts_batch_at_exactly_max_events() -> None:
    event_dict = _valid_event().model_dump(mode="json")
    payload = {"source": "test-suite", "events": [event_dict] * MAX_BATCH_EVENTS}

    response = client.post("/api/mock/regengine/ingest", json=payload)

    assert response.status_code == 200
    assert response.json()["total"] == MAX_BATCH_EVENTS


# --- #120: idempotency cache must expire replays after 24h -----------------


def test_default_idempotency_ttl_matches_24h_parity_claim() -> None:
    # Regression guard for the README/docstring's "24-hour idempotency
    # replays" claim actually being what the default construction does.
    service = MockRegEngineService()
    assert service.idempotency_ttl == timedelta(hours=24)
    assert IDEMPOTENCY_TTL_HOURS == 24


def test_idempotency_replay_respects_ttl_boundary_without_sleeping() -> None:
    # A mutable holder + injected clock lets the test advance time exactly
    # across the 24h boundary with no real elapsed time and no sleep call.
    clock_state = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    service = MockRegEngineService(clock=lambda: clock_state["now"])
    payload = IngestPayload(source="ttl-test", events=[_valid_event()])

    first = service.ingest(payload, idempotency_key="ttl-boundary-key")

    # Just under the window: the cached response still replays verbatim.
    clock_state["now"] += timedelta(hours=24) - timedelta(seconds=1)
    still_cached = service.ingest(payload, idempotency_key="ttl-boundary-key")
    assert still_cached is first

    # At/over the window: RegEngine treats the key as unseen, so this
    # re-ingests as a brand new event rather than replaying the stale one.
    clock_state["now"] += timedelta(seconds=2)
    fresh = service.ingest(payload, idempotency_key="ttl-boundary-key")
    assert fresh is not first
    assert fresh.events[0].event_id != first.events[0].event_id


def test_idempotency_cache_still_replays_within_default_ttl() -> None:
    # Same-instant repeat call under the real default (unmocked) clock --
    # protects the existing test_mock_idempotency_replay_returns_cached_response
    # coverage in tests/test_integration_settings.py from regressing.
    service = MockRegEngineService()
    payload = IngestPayload(source="ttl-test", events=[_valid_event()])

    first = service.ingest(payload, idempotency_key="same-instant-key")
    second = service.ingest(payload, idempotency_key="same-instant-key")

    assert first is second


# --- #122: chain_hash must resume from disk instead of forking on restart --


def test_fresh_service_resumes_chain_hash_from_persisted_store(tmp_path) -> None:
    persist_path = tmp_path / "resume-events.jsonl"

    # Ground truth: a service that ingests two events back-to-back without
    # ever restarting. A resumed service must land on the same chain_hash
    # for the second event that this one does.
    continuous_service = MockRegEngineService()
    seed_payload = IngestPayload(source="resume-test", events=[_valid_event(lot="TLC-RESUME-SEED")])
    seed_event = continuous_service.ingest(seed_payload).events[0]
    assert seed_event.status == "accepted"
    assert seed_event.chain_hash

    next_payload = IngestPayload(source="resume-test", events=[_valid_event(lot="TLC-RESUME-NEXT")])
    continuous_next_chain_hash = continuous_service.ingest(next_payload).events[0].chain_hash

    # Persist only the seed event's response, exactly as SimulationController
    # writes StoredEventRecord.delivery_response after a real step(), then
    # simulate a process restart: a brand new EventStore reading that file
    # and a brand new MockRegEngineService seeded from it.
    store = EventStore(persist_path=str(persist_path))
    store.add_many(
        [
            StoredEventRecord(
                payload_source="resume-test",
                event=seed_payload.events[0],
                destination_mode=DestinationMode.MOCK,
                delivery_status="posted",
                delivery_response=seed_event.model_dump(mode="json"),
            )
        ]
    )
    restarted_store = EventStore(persist_path=str(persist_path))
    restarted_service = MockRegEngineService(store=restarted_store)
    restarted_next_chain_hash = restarted_service.ingest(next_payload).events[0].chain_hash

    # The restarted service continues the exact lineage the never-restarted
    # service did -- not a fork from "".
    assert restarted_next_chain_hash == continuous_next_chain_hash

    # And explicitly not what an unseeded fresh service produces for the
    # identical event -- that mismatch is the bug #122 describes.
    unseeded_chain_hash = MockRegEngineService().ingest(next_payload).events[0].chain_hash
    assert restarted_next_chain_hash != unseeded_chain_hash


def test_resume_with_no_persisted_records_starts_empty_like_before(tmp_path) -> None:
    empty_store = EventStore(persist_path=str(tmp_path / "empty-events.jsonl"))

    seeded_service = MockRegEngineService(store=empty_store)
    unseeded_service = MockRegEngineService()
    payload = IngestPayload(source="resume-test", events=[_valid_event(lot="TLC-RESUME-EMPTY")])

    assert (
        seeded_service.ingest(payload).events[0].chain_hash
        == unseeded_service.ingest(payload).events[0].chain_hash
    )


def test_resume_ignores_live_delivered_and_rejected_records(tmp_path) -> None:
    persist_path = tmp_path / "mixed-events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many(
        [
            StoredEventRecord(
                payload_source="resume-test",
                event=_valid_event(lot="TLC-RESUME-LIVE"),
                destination_mode=DestinationMode.LIVE,
                delivery_status="posted",
                # A live delivery could coincidentally carry a chain_hash-shaped
                # key -- it must never be mistaken for the mock's own lineage.
                delivery_response={"chain_hash": "live-chain-hash-should-not-be-picked-up"},
            ),
            StoredEventRecord(
                payload_source="resume-test",
                event=_valid_event(lot="TLC-RESUME-REJECTED"),
                destination_mode=DestinationMode.MOCK,
                delivery_status="failed",
                delivery_response={"status": "rejected", "chain_hash": None, "errors": ["bad event"]},
            ),
        ]
    )

    resumed_store = EventStore(persist_path=str(persist_path))
    seeded_service = MockRegEngineService(store=resumed_store)
    unseeded_service = MockRegEngineService()
    payload = IngestPayload(source="resume-test", events=[_valid_event(lot="TLC-RESUME-CHECK")])

    assert (
        seeded_service.ingest(payload).events[0].chain_hash
        == unseeded_service.ingest(payload).events[0].chain_hash
    )

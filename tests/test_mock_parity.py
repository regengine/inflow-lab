"""Mock-vs-live parity tests for the in-process RegEngine stand-in.

These cover the branches that only exist so the mock behaves like the live
webhook: the 1-500 batch bounds, in-batch duplicate rejection, HMAC signature
verification, the 24h idempotency replay window (and its capacity bound), and
chain-hash continuity across a process restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.mock_service import (
    _IDEMPOTENCY_CACHE_LIMIT,
    IDEMPOTENCY_TTL,
    MAX_BATCH_EVENTS,
    MockRegEngineHTTPError,
    MockRegEngineService,
    chain_hash_from_records,
)
from app.regengine_client import WEBHOOK_HMAC_SECRET_ENV
from app.schemas.domain import StoredEventRecord
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig
from app.store import EventStore


client = TestClient(app)

HMAC_SECRET = "mock-parity-shared-secret"


def setup_function() -> None:
    import asyncio

    asyncio.run(controller.reset(SimulationConfig()))


# Payloads are built relative to "now" so they always sit inside RegEngine's
# replay window (WEBHOOK_MAX_EVENT_AGE_DAYS=90). Pinned calendar dates went
# stale and made every parity payload a rejection once the mock started
# enforcing the floor by default.
EVENT_AGE = timedelta(days=1)


#: Resolved once per run so two payloads built in the same test are byte-equal
#: — the in-batch duplicate check keys on (CTE, lot, timestamp, location).
BASE_MOMENT = (datetime.now(UTC) - EVENT_AGE).replace(microsecond=0)


def event_moment(offset: timedelta = timedelta(0)) -> datetime:
    """A recent, in-replay-window instant; `offset` moves it forward."""
    return BASE_MOMENT + offset


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def receiving_event(lot_code: str = "TLC-PARITY-000001", **overrides) -> dict:
    moment = event_moment()
    event = {
        "cte_type": "receiving",
        "traceability_lot_code": lot_code,
        "product_description": "Romaine Lettuce",
        "quantity": 500,
        "unit_of_measure": "cases",
        "location_name": "Distribution Center #4",
        "timestamp": iso(moment),
        "kdes": {
            "receive_date": moment.date().isoformat(),
            "receiving_location": "Distribution Center #4",
            "ship_from_location": "Valley Fresh Farms",
            "immediate_previous_source": "Valley Fresh Farms",
            "reference_document": "Bill of Lading BOL-PARITY-0001",
            "tlc_source_reference": "SRC-PARITY-0001",
        },
    }
    event.update(overrides)
    return event


def payload_dict(*events: dict, source: str = "mock-parity") -> dict:
    return {"source": source, "events": list(events)}


def ingest_payload(*events: dict, source: str = "mock-parity") -> IngestPayload:
    return IngestPayload.model_validate(payload_dict(*events, source=source))


def canonical_body(payload: dict) -> bytes:
    """Serialize exactly the way LiveRegEngineClient signs the wire body."""
    return json.dumps(
        IngestPayload.model_validate(payload).model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def signature_for(body: bytes, secret: str = HMAC_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- #118 / #142: batch bounds ------------------------------------------------


def test_mock_service_rejects_empty_event_batch():
    service = MockRegEngineService()
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        service.ingest(ingest_payload())
    assert excinfo.value.status_code == 422
    assert "1-500 items per batch" in excinfo.value.detail


def test_empty_batch_over_http_returns_422_not_200():
    response = client.post("/api/mock/regengine/ingest", json=payload_dict())
    assert response.status_code == 422
    assert "per batch" in response.json()["detail"]


def test_oversized_batch_over_http_returns_422_with_cap_message():
    events = [receiving_event(f"TLC-PARITY-CAP-{index:04d}") for index in range(MAX_BATCH_EVENTS + 1)]
    response = client.post("/api/mock/regengine/ingest", json=payload_dict(*events))
    assert response.status_code == 422
    assert response.json()["detail"] == f"events accepts at most {MAX_BATCH_EVENTS} items per batch"


def test_batch_at_the_cap_is_still_accepted():
    events = [receiving_event(f"TLC-PARITY-MAX-{index:04d}") for index in range(MAX_BATCH_EVENTS)]
    response = client.post("/api/mock/regengine/ingest", json=payload_dict(*events))
    assert response.status_code == 200
    assert response.json()["accepted"] == MAX_BATCH_EVENTS


# --- #123: in-batch duplicate rejection ---------------------------------------


def test_duplicate_event_in_batch_is_rejected_once():
    duplicate = receiving_event("TLC-PARITY-DUP-01")
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(duplicate, dict(duplicate)),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 1
    assert body["events"][0]["status"] == "accepted"
    assert body["events"][1]["status"] == "rejected"
    assert body["events"][1]["errors"] == [
        "Duplicate event in batch (same CTE, lot, timestamp, and location)"
    ]


def test_same_lot_at_a_different_timestamp_is_not_a_duplicate():
    first = receiving_event("TLC-PARITY-DUP-02")
    second = receiving_event("TLC-PARITY-DUP-02", timestamp=iso(event_moment(timedelta(hours=1))))
    response = client.post("/api/mock/regengine/ingest", json=payload_dict(first, second))
    assert response.status_code == 200
    assert response.json()["accepted"] == 2


# --- #116: Idempotency-Key and friction over HTTP -----------------------------


def test_http_route_replays_the_cached_response_for_a_repeated_idempotency_key():
    body = payload_dict(receiving_event("TLC-PARITY-IDEM-01"))
    headers = {"Idempotency-Key": "parity-key-001"}

    first = client.post("/api/mock/regengine/ingest", json=body, headers=headers)
    second = client.post("/api/mock/regengine/ingest", json=body, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    # A replay must not advance the shared chain hash.
    assert first.json()["events"][0]["chain_hash"] == second.json()["events"][0]["chain_hash"]


def test_http_route_without_an_idempotency_key_still_double_processes():
    body = payload_dict(receiving_event("TLC-PARITY-IDEM-02"))
    first = client.post("/api/mock/regengine/ingest", json=body)
    second = client.post("/api/mock/regengine/ingest", json=body)
    assert first.json()["events"][0]["chain_hash"] != second.json()["events"][0]["chain_hash"]


@pytest.mark.parametrize(
    ("code", "status"),
    [("invalid_key", 401), ("subscription_inactive", 402), ("rate_limit", 429)],
)
def test_http_route_honours_mock_friction_codes(code: str, status: int):
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(receiving_event("TLC-PARITY-FRICTION")),
        headers={"X-Mock-Friction": code},
    )
    assert response.status_code == status
    assert response.json()["detail"]


def test_http_route_ignores_blank_friction_header():
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(receiving_event("TLC-PARITY-FRICTION-NONE")),
        headers={"X-Mock-Friction": " , "},
    )
    assert response.status_code == 200


# --- #113: HMAC signature verification ----------------------------------------


def test_unsigned_request_succeeds_when_no_secret_is_configured(monkeypatch):
    monkeypatch.delenv(WEBHOOK_HMAC_SECRET_ENV, raising=False)
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(receiving_event("TLC-PARITY-HMAC-OFF")),
    )
    assert response.status_code == 200


def test_correctly_signed_request_succeeds_when_a_secret_is_configured(monkeypatch):
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    body = payload_dict(receiving_event("TLC-PARITY-HMAC-OK"))
    signed_bytes = canonical_body(body)
    response = client.post(
        "/api/mock/regengine/ingest",
        content=signed_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_for(signed_bytes),
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_missing_signature_is_rejected_when_a_secret_is_configured(monkeypatch):
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(receiving_event("TLC-PARITY-HMAC-MISSING")),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing X-Webhook-Signature header"


def test_signature_over_drifted_body_bytes_is_rejected(monkeypatch):
    """The exact bug docs/HMAC_STAGING_VALIDATION.md calls "Body-Bytes Drift"."""
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    body = payload_dict(receiving_event("TLC-PARITY-HMAC-DRIFT"))
    wire_bytes = json.dumps(IngestPayload.model_validate(body).model_dump(mode="json")).encode("utf-8")
    # Signed over the canonical serialization, sent as the pretty one.
    response = client.post(
        "/api/mock/regengine/ingest",
        content=wire_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_for(canonical_body(body)),
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid webhook signature"


def test_signature_from_the_wrong_secret_is_rejected(monkeypatch):
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    body = payload_dict(receiving_event("TLC-PARITY-HMAC-WRONG"))
    signed_bytes = canonical_body(body)
    response = client.post(
        "/api/mock/regengine/ingest",
        content=signed_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_for(signed_bytes, secret="not-the-secret"),
        },
    )
    assert response.status_code == 401


def test_verify_signature_accepts_a_bare_hex_digest(monkeypatch):
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    service = MockRegEngineService()
    body = b'{"events":[]}'
    bare = signature_for(body).removeprefix("sha256=")
    service.verify_signature(body, bare)  # must not raise


# --- #120: 24h idempotency window ---------------------------------------------


def test_idempotency_replay_just_inside_the_window():
    service = MockRegEngineService()
    # A frozen clock just after the payload's own timestamp: the events are
    # built relative to "now", so a pinned calendar instant would read them as
    # future-dated and 422 the batch.
    now = event_moment(timedelta(hours=1))
    service.time_source = lambda: now
    first = service.ingest(ingest_payload(receiving_event("TLC-PARITY-TTL-01")), idempotency_key="ttl-1")

    service.time_source = lambda: now + IDEMPOTENCY_TTL - timedelta(minutes=1)
    replay = service.ingest(ingest_payload(receiving_event("TLC-PARITY-TTL-01")), idempotency_key="ttl-1")
    assert replay is first


def test_idempotency_entry_expires_just_outside_the_window():
    service = MockRegEngineService()
    # A frozen clock just after the payload's own timestamp: the events are
    # built relative to "now", so a pinned calendar instant would read them as
    # future-dated and 422 the batch.
    now = event_moment(timedelta(hours=1))
    service.time_source = lambda: now
    first = service.ingest(ingest_payload(receiving_event("TLC-PARITY-TTL-02")), idempotency_key="ttl-2")

    service.time_source = lambda: now + IDEMPOTENCY_TTL + timedelta(minutes=1)
    reingested = service.ingest(
        ingest_payload(receiving_event("TLC-PARITY-TTL-02")), idempotency_key="ttl-2"
    )
    assert reingested is not first
    # Expired, so the event really was ingested again and the chain advanced.
    assert reingested.events[0].chain_hash != first.events[0].chain_hash


def test_expired_entries_are_dropped_from_the_cache():
    service = MockRegEngineService()
    # A frozen clock just after the payload's own timestamp: the events are
    # built relative to "now", so a pinned calendar instant would read them as
    # future-dated and 422 the batch.
    now = event_moment(timedelta(hours=1))
    service.time_source = lambda: now
    service.ingest(ingest_payload(receiving_event("TLC-PARITY-TTL-03")), idempotency_key="ttl-3")
    assert len(service._idempotency_cache) == 1

    service.time_source = lambda: now + IDEMPOTENCY_TTL + timedelta(seconds=1)
    service.ingest(ingest_payload(receiving_event("TLC-PARITY-TTL-04")), idempotency_key="ttl-4")
    assert "ttl-3" not in service._idempotency_cache


# --- #123: idempotency cache eviction -----------------------------------------


def test_idempotency_cache_evicts_the_oldest_key_past_the_capacity_limit():
    service = MockRegEngineService()
    payload = ingest_payload(receiving_event("TLC-PARITY-EVICT"))

    oldest = service.ingest(payload, idempotency_key="evict-key-0")
    for index in range(1, _IDEMPOTENCY_CACHE_LIMIT + 1):
        service.ingest(payload, idempotency_key=f"evict-key-{index}")

    assert len(service._idempotency_cache) == _IDEMPOTENCY_CACHE_LIMIT
    assert "evict-key-0" not in service._idempotency_cache
    assert f"evict-key-{_IDEMPOTENCY_CACHE_LIMIT}" in service._idempotency_cache

    # The evicted key is a cache miss: it re-ingests instead of replaying.
    replayed = service.ingest(payload, idempotency_key="evict-key-0")
    assert replayed is not oldest


def test_replaying_a_key_keeps_it_from_being_evicted():
    service = MockRegEngineService()
    payload = ingest_payload(receiving_event("TLC-PARITY-LRU"))
    first = service.ingest(payload, idempotency_key="lru-key")
    for index in range(_IDEMPOTENCY_CACHE_LIMIT - 1):
        service.ingest(payload, idempotency_key=f"filler-{index}")
        service.ingest(payload, idempotency_key="lru-key")
    assert service.ingest(payload, idempotency_key="lru-key") is first


# --- #122: chain-hash continuity across a restart -----------------------------


def stored_record_with_chain_hash(chain_hash: str, sequence_no: int = 1) -> StoredEventRecord:
    return StoredEventRecord(
        sequence_no=sequence_no,
        payload_source="mock-parity",
        event=ingest_payload(receiving_event(f"TLC-PARITY-CHAIN-{sequence_no:03d}")).events[0],
        delivery_status="posted",
        delivery_response={
            "traceability_lot_code": f"TLC-PARITY-CHAIN-{sequence_no:03d}",
            "cte_type": "receiving",
            "status": "accepted",
            "sha256_hash": "0" * 64,
            "chain_hash": chain_hash,
        },
    )


def test_chain_hash_from_records_picks_the_highest_sequence_number():
    records = [
        stored_record_with_chain_hash("aaa", sequence_no=1),
        stored_record_with_chain_hash("ccc", sequence_no=3),
        stored_record_with_chain_hash("bbb", sequence_no=2),
    ]
    assert chain_hash_from_records(records) == "ccc"
    assert chain_hash_from_records([]) == ""


def test_chain_hash_from_records_ignores_rejected_and_undelivered_records():
    undelivered = stored_record_with_chain_hash("aaa", sequence_no=1)
    undelivered.delivery_response = None
    rejected = stored_record_with_chain_hash("aaa", sequence_no=2)
    rejected.delivery_response = {"status": "rejected", "errors": ["nope"]}
    assert chain_hash_from_records([undelivered, rejected]) == ""


def test_restarted_service_resumes_the_persisted_chain(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    original = MockRegEngineService(event_source=store)
    first = original.ingest(ingest_payload(receiving_event("TLC-PARITY-RESTART-01")))
    store.add_many([stored_record_with_chain_hash(first.events[0].chain_hash)])

    # A restart: fresh service, same on-disk event log.
    restarted = MockRegEngineService(event_source=EventStore(persist_path=str(persist_path)))
    assert restarted.chain_hash == ""  # not yet resumed — resumption is lazy
    resumed = restarted.ingest(ingest_payload(receiving_event("TLC-PARITY-RESTART-02")))

    continued = original.ingest(ingest_payload(receiving_event("TLC-PARITY-RESTART-02")))
    assert resumed.events[0].chain_hash == continued.events[0].chain_hash


def test_service_without_an_event_source_starts_a_fresh_chain(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([stored_record_with_chain_hash("deadbeef")])

    detached = MockRegEngineService()
    detached.ingest(ingest_payload(receiving_event("TLC-PARITY-DETACHED")))

    attached = MockRegEngineService(event_source=store)
    attached.ingest(ingest_payload(receiving_event("TLC-PARITY-DETACHED")))
    assert detached.chain_hash != attached.chain_hash


def test_reset_clears_the_chain_and_the_idempotency_cache(tmp_path):
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.add_many([stored_record_with_chain_hash("deadbeef")])
    service = MockRegEngineService(event_source=store)
    service.ingest(ingest_payload(receiving_event("TLC-PARITY-RESET")), idempotency_key="reset-key")
    assert service.chain_hash

    service.reset()
    assert service.chain_hash == ""
    assert not service._idempotency_cache
    # The persisted log is reset alongside the service, so no stale resume.
    service.ingest(ingest_payload(receiving_event("TLC-PARITY-RESET")))
    fresh = MockRegEngineService()
    fresh.ingest(ingest_payload(receiving_event("TLC-PARITY-RESET")))
    assert service.chain_hash == fresh.chain_hash

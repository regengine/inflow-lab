"""Coverage for #95 (retry replays the original batch's Idempotency-Key)
and #103 (CSV import/replay post one un-chunked, uncapped batch).

Both bugs are about how a batch of events actually reaches RegEngine, so
they are covered together here rather than split across two files.

#95 has three independently-checkable pieces, each its own test below:
  - a per-event rejection (HTTP 2xx, one event's own verdict is
    "rejected") must actually be redeliverable once corrected -- the
    core bug: retry used to unconditionally reuse the original batch's
    Idempotency-Key, so RegEngine's idempotency cache answered from the
    stale (still-rejecting) cached response without ever revalidating
    anything.
  - a genuine transport failure (no response came back at all) must
    still reuse its stored key on retry -- that behavior is deliberate
    (see tests/test_api.py::test_live_delivery_retry_reuses_original_idempotency_key,
    which this file must not disturb) and is re-pinned here from a
    fresh angle.
  - a response that turns out to answer a different-sized request than
    the one just sent (a stale idempotency-cache replay slipping past
    the above) must not have its counts folded into `posted`.

#103 is chunking `import_csv`/`replay` at RegEngine's real
MAX_BATCH_EVENTS cap, and specifically that a failure partway through
several chunks reports partial progress instead of erasing the chunks
that already succeeded.

Kept in its own file rather than added to an existing one -- strict
per-file ownership across parallel workstreams means existing test
files must not be edited, and app/controller.py plus this file are the
only files this workstream owns.
"""

from __future__ import annotations

import asyncio

import app.controller as controller_module
from app.main import controller
from app.mock_service import MAX_BATCH_EVENTS
from app.regengine_client import LiveIngestResult, LiveRegEngineDeliveryError
from app.schemas.domain import (
    CSVImportType,
    CTEType,
    DestinationMode,
    RegEngineEvent,
    StoredEventRecord,
)
from app.schemas.ingestion import CSVImportRequest, DeliveryRetryRequest, IngestPayload, ReplayRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from tests.support.timestamps import recent_event_timestamp


_BASE_TIME = recent_event_timestamp()
_VALID_KDES = {"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-BATCHING-DEFAULT"}


def setup_function() -> None:
    # app/main.py's `controller` is a module-level singleton shared by
    # every test file in the process (same convention as
    # tests/test_api.py, tests/test_api_robustness.py, and
    # tests/test_delivery_retry_paths.py) -- reset it before each test so
    # records (and the mock's idempotency cache) left behind by another
    # test can't leak in.
    asyncio.run(controller.reset(SimulationConfig()))


def _harvesting_event(lot_code: str, *, kdes: dict | None = None) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot_code,
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=_BASE_TIME,
        kdes=_VALID_KDES if kdes is None else kdes,
    )


def _seed_lot_csv(lot_codes: list[str]) -> str:
    """A minimal seed_lots CSV that survives mock validation for every row.

    seed_lots imports become HARVESTING events (app/csv_importer.py's
    _parse_seed_lot), which require a `reference_document` KDE
    (app/cte_rules.py's REQUIRED_KDES); the auto-filled
    reference_document_type/_number _parse_seed_lot adds do not satisfy
    that (mock_service.py's strict, non-aliasing lookup), so a bare
    `reference_document` column is added explicitly.
    """
    header = "traceability_lot_code,product_description,quantity,unit_of_measure,location_name,reference_document"
    rows = [
        f"{lot},Romaine Hearts,10,cases,Valley Fresh Farms,HL-{lot}"
        for lot in lot_codes
    ]
    return "\n".join([header, *rows]) + "\n"


# ---------------------------------------------------------------------------
# #95 -- a per-event rejection must actually be redeliverable once corrected.
# ---------------------------------------------------------------------------


def test_per_event_rejection_redelivers_on_retry_once_corrected():
    """Reproduces #95's exact reported shape end to end through the real
    mock service and retry_failed_delivery: a two-event batch posted
    under one Idempotency-Key, one event rejected for missing KDEs. The
    operator corrects that event's KDEs in the store and retries. Before
    #95's fix, retry unconditionally reused the original batch's key, so
    the mock's idempotency cache replayed the stale (still-rejecting)
    response without ever looking at the corrected event -- the record
    stayed failed forever. It must now come back posted.
    """
    good_event = _harvesting_event("TLC-BATCH95-GOOD")
    bad_event = _harvesting_event("TLC-BATCH95-BAD", kdes={})  # missing KDEs -> rejected

    original_key = "fixed-original-batch-key-95"
    response = controller.mock_service.ingest(
        IngestPayload(source="batching-suite", events=[good_event, bad_event]),
        idempotency_key=original_key,
    )
    assert response.accepted == 1
    assert response.rejected == 1
    # mock_service appends accepted/rejected in a single pass as it goes
    # (see _pair_event_responses's own docstring on why this is the one
    # place the response *is* in request order), so events[0]/[1] line up
    # with good_event/bad_event exactly.
    good_verdict, bad_verdict = (event.model_dump(mode="json") for event in response.events)
    assert good_verdict["status"] == "accepted"
    assert bad_verdict["status"] == "rejected"

    good_record = StoredEventRecord(
        payload_source="batching-suite",
        event=good_event,
        destination_mode=DestinationMode.MOCK,
        delivery_status="posted",
        delivery_attempts=1,
        delivery_response=good_verdict,
        delivery_metadata={"delivery_mode": "mock", "idempotency_key": original_key},
        last_delivery_success_at=_BASE_TIME,
    )
    bad_record = StoredEventRecord(
        payload_source="batching-suite",
        event=bad_event,
        destination_mode=DestinationMode.MOCK,
        delivery_status="failed",
        delivery_attempts=1,
        error="Missing required KDEs for harvesting: harvest_date, reference_document",
        delivery_response=bad_verdict,
        delivery_metadata={"delivery_mode": "mock", "idempotency_key": original_key},
    )
    controller.store.add_many([good_record, bad_record])

    # The operator corrects the rejected event: same lot/CTE identity,
    # now carrying the KDEs the mock required.
    corrected = bad_record.model_copy(update={"event": _harvesting_event("TLC-BATCH95-BAD")})
    controller.store.update_many([corrected])

    retry_response = asyncio.run(
        controller.retry_failed_delivery(
            DeliveryRetryRequest(
                record_ids=[bad_record.record_id],
                delivery=DeliveryConfig(mode=DestinationMode.MOCK),
            )
        )
    )

    # The acceptance bar #95 itself states: the record's delivery_status
    # becomes "posted" once retried after correction.
    assert retry_response.status == "posted"
    assert retry_response.posted == 1
    assert retry_response.failed == 0

    stored = {record.record_id: record for record in controller.store.all_between()}
    assert stored[bad_record.record_id].delivery_status == "posted"
    assert stored[bad_record.record_id].error is None
    assert stored[bad_record.record_id].delivery_attempts == 2


# ---------------------------------------------------------------------------
# #95 -- a genuine transport failure must still reuse its stored key.
# ---------------------------------------------------------------------------


def test_transport_failure_retry_still_reuses_stored_key(monkeypatch):
    """A record whose original attempt produced no response at all
    (delivery_response is None -- a pure transport failure) is exactly
    the case #95's fix keeps reusing the stored key for: retrying under
    the same key is a legitimate "did this already land" idempotency
    check, not a bug. Re-pins that from a fresh angle (independent of
    tests/test_api.py's own version of this, which this workstream may
    not edit).
    """
    asyncio.run(
        controller.reset(
            SimulationConfig(delivery=DeliveryConfig(mode=DestinationMode.LIVE, api_key="k", tenant_id="t"))
        )
    )

    original_key = "transport-failure-key-95"
    failed_record = StoredEventRecord(
        payload_source="batching-suite",
        event=_harvesting_event("TLC-TRANSPORT-RETRY-95"),
        destination_mode=DestinationMode.LIVE,
        delivery_status="failed",
        delivery_attempts=1,
        error="connection reset",
        delivery_response=None,
        delivery_metadata={"delivery_mode": "live", "idempotency_key": original_key},
    )
    controller.store.add_many([failed_record])

    seen_keys: list[str | None] = []

    class RecordingLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            seen_keys.append(idempotency_key)
            return LiveIngestResult(
                response={
                    "accepted": len(payload.events),
                    "rejected": 0,
                    "events": [
                        {
                            "traceability_lot_code": event.traceability_lot_code,
                            "cte_type": event.cte_type.value,
                            "status": "accepted",
                        }
                        for event in payload.events
                    ],
                },
                metadata={"delivery_mode": "live", "idempotency_key": idempotency_key, "status_code": 200},
            )

    monkeypatch.setattr(controller, "live_client", RecordingLiveClient())

    retry_response = asyncio.run(
        controller.retry_failed_delivery(
            DeliveryRetryRequest(delivery=DeliveryConfig(mode=DestinationMode.LIVE, api_key="k", tenant_id="t"))
        )
    )

    assert seen_keys == [original_key]
    assert retry_response.status == "posted"
    assert retry_response.posted == 1
    assert retry_response.failed == 0


# ---------------------------------------------------------------------------
# #95 -- a replayed response's counts must never be folded into `posted`.
# ---------------------------------------------------------------------------


def test_replayed_response_is_not_counted_as_freshly_posted(monkeypatch):
    """Isolates _deliver_payload's own defensive check: a response sized
    for a different (larger) request than the one just sent -- exactly
    what a stale idempotency-cache replay looks like -- must not have its
    accepted count folded into DeliveryOutcome.posted. Drives
    _deliver_payload directly rather than through retry_failed_delivery
    so this pins that one mechanism regardless of how a caller reached
    it.
    """
    asyncio.run(
        controller.reset(
            SimulationConfig(delivery=DeliveryConfig(mode=DestinationMode.LIVE, api_key="k", tenant_id="t"))
        )
    )

    class StaleReplayLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            # Answers with a response sized for a 3-event batch no matter
            # how many events this particular request carries -- the
            # shape a mismatched idempotency-cache replay produces.
            return LiveIngestResult(
                response={
                    "accepted": 3,
                    "rejected": 0,
                    "events": [
                        {
                            "traceability_lot_code": f"TLC-STALE-{index}",
                            "cte_type": "harvesting",
                            "status": "accepted",
                        }
                        for index in range(3)
                    ],
                },
                metadata={"delivery_mode": "live", "idempotency_key": idempotency_key, "status_code": 200},
            )

    monkeypatch.setattr(controller, "live_client", StaleReplayLiveClient())

    payload = IngestPayload(source="batching-suite", events=[_harvesting_event("TLC-REPLAY-ONLY-95")])
    outcome = asyncio.run(
        controller._deliver_payload(payload, controller.config, idempotency_key="reused-key-95")
    )

    # The stale response claims accepted=3; none of that may land in
    # posted for a request that only sent one event.
    assert outcome.posted == 0
    assert outcome.failed == 1
    assert outcome.delivery_status == "failed"
    assert outcome.metadata is not None
    assert outcome.metadata.get("idempotency_replay") is True


# ---------------------------------------------------------------------------
# #103 -- an import over the cap succeeds in chunks.
# ---------------------------------------------------------------------------


def test_csv_import_over_the_cap_succeeds_in_chunks():
    """A CSV import of MAX_BATCH_EVENTS + 100 valid rows must deliver
    every row rather than 422 the whole batch (#103's core acceptance
    criterion), split across at least two requests each carrying its own
    Idempotency-Key.
    """
    lot_codes = [f"TLC-BULK95-{index:06d}" for index in range(MAX_BATCH_EVENTS + 100)]
    request = CSVImportRequest(
        import_type=CSVImportType.SEED_LOTS,
        csv_text=_seed_lot_csv(lot_codes),
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )

    response = asyncio.run(controller.import_csv(request))

    assert response.errors == []
    assert response.accepted == len(lot_codes)
    assert response.stored == len(lot_codes)
    assert response.posted == len(lot_codes)
    assert response.failed == 0
    assert response.status == "accepted"

    stored = controller.store.all_between()
    assert len(stored) == len(lot_codes)
    assert all(record.delivery_status == "posted" for record in stored)

    # Genuine chunking, not one lucky oversized request: at least two
    # distinct Idempotency-Keys were actually used.
    keys = {
        record.delivery_metadata.get("idempotency_key")
        for record in stored
        if record.delivery_metadata
    }
    assert len(keys) >= 2


def test_replay_of_over_cap_store_succeeds_in_chunks():
    """replay() faces the identical cap on the read side -- #103 reports
    it as permanently broken once a persisted store passes
    MAX_BATCH_EVENTS records, since every replay attempt would re-hit
    the same 422 forever. Must complete without one, chunked the same
    way import_csv is above.
    """
    total = MAX_BATCH_EVENTS + 50
    records = [
        StoredEventRecord(
            payload_source="batching-suite",
            event=_harvesting_event(f"TLC-REPLAYBULK95-{index:06d}"),
            destination_mode=DestinationMode.NONE,
            delivery_status="generated",
        )
        for index in range(total)
    ]
    controller.store.add_many(records)

    response = asyncio.run(controller.replay(ReplayRequest(delivery=DeliveryConfig(mode=DestinationMode.MOCK))))

    assert response.status == "posted"
    assert response.read == total
    assert response.replayed == total
    assert response.posted == total
    assert response.failed == 0


# ---------------------------------------------------------------------------
# #103 -- a mid-run chunk failure reports partial progress, not a wipeout.
# ---------------------------------------------------------------------------


def test_mid_chunk_failure_reports_partial_progress_not_discarded(monkeypatch):
    """Shrinks MAX_BATCH_EVENTS to 2 on the controller module only (the
    mock's own real 500-event cap in app/mock_service.py is untouched,
    and a 2-event chunk always clears it trivially), so a small, fast
    event list still produces several chunks. The 2nd of 4 chunks fails
    outright; chunks 1, 3, and 4 must still be reported as posted --
    an earlier chunk's success (and a later chunk's own, independent
    success) must not be erased by one chunk in the middle failing.
    """
    monkeypatch.setattr(controller_module, "MAX_BATCH_EVENTS", 2)
    asyncio.run(
        controller.reset(
            SimulationConfig(delivery=DeliveryConfig(mode=DestinationMode.LIVE, api_key="k", tenant_id="t"))
        )
    )

    class PartiallyFailingLiveClient:
        def __init__(self) -> None:
            self.calls = 0

        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 2:
                raise LiveRegEngineDeliveryError(
                    "simulated outage on chunk 2",
                    {"delivery_mode": "live", "idempotency_key": idempotency_key, "status_code": 503},
                )
            return LiveIngestResult(
                response={
                    "accepted": len(payload.events),
                    "rejected": 0,
                    "events": [
                        {
                            "traceability_lot_code": event.traceability_lot_code,
                            "cte_type": event.cte_type.value,
                            "status": "accepted",
                        }
                        for event in payload.events
                    ],
                },
                metadata={"delivery_mode": "live", "idempotency_key": idempotency_key, "status_code": 200},
            )

    fake_client = PartiallyFailingLiveClient()
    monkeypatch.setattr(controller, "live_client", fake_client)

    lot_codes = [f"TLC-CHUNKFAIL95-{index:02d}" for index in range(7)]
    request = CSVImportRequest(
        import_type=CSVImportType.SEED_LOTS,
        csv_text=_seed_lot_csv(lot_codes),
        delivery=DeliveryConfig(mode=DestinationMode.LIVE, api_key="k", tenant_id="t"),
    )

    response = asyncio.run(controller.import_csv(request))

    # 7 events / chunk size 2 -> chunks of [2, 2, 2, 1] = 4 requests; the
    # 2nd chunk's two events fail outright, the other three chunks' five
    # events do not.
    assert fake_client.calls == 4
    assert response.stored == 7  # every row still has a record
    assert response.posted == 5  # chunks 1, 3, 4
    assert response.failed == 2  # chunk 2 only
    assert response.status == "delivery_failed"

    stored = controller.store.all_between()
    assert len(stored) == 7
    posted_lots = {record.event.traceability_lot_code for record in stored if record.delivery_status == "posted"}
    failed_lots = {record.event.traceability_lot_code for record in stored if record.delivery_status == "failed"}
    assert posted_lots == {lot_codes[0], lot_codes[1], lot_codes[4], lot_codes[5], lot_codes[6]}
    assert failed_lots == {lot_codes[2], lot_codes[3]}

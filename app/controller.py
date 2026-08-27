from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import urlparse

from fastapi import HTTPException

from .audit import summarize_scenario_audit
from .contract import INFLOW_CONTRACT_VERSION
from .csv_importer import parse_csv_import
from .demo_fixtures import get_demo_fixture
from .engine import LegitFlowEngine
from .mock_service import MAX_BATCH_EVENTS, MockRegEngineHTTPError, MockRegEngineService
from .schemas.domain import DemoFixtureId, DestinationMode, RegEngineEvent, StoredEventRecord
from .schemas.ingestion import (
    CSVImportRequest,
    CSVImportResponse,
    DeliveryRetryRequest,
    DeliveryRetryResponse,
    IngestPayload,
    ReplayRequest,
    ReplayResponse,
)
from .schemas.scenarios import (
    DemoFixtureLoadRequest,
    DemoFixtureLoadResponse,
    ScenarioLoadResponse,
    ScenarioSaveListResponse,
    ScenarioSaveRequest,
    ScenarioSaveResponse,
    ScenarioSaveSnapshot,
    ScenarioSaveSummary,
)
from .schemas.simulation import (
    DeliveryConfig,
    SimulationConfig,
    StepResponse,
)
from .regengine_client import (
    DEFAULT_LIVE_INGEST_ENDPOINT,
    WEBHOOK_HMAC_SECRET_ENV,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
)
from .schemas.integration import IntegrationConfigureRequest, IntegrationStatusResponse
from .scenario_saves import ScenarioSaveStore
from .scenarios import ScenarioId, get_scenario
from .store import EventStore, mask_secret_in_payload, mask_secret_in_string


# Same name-keyed singleton logger main.py configures, so delivery
# failures land in the stream operators already watch (#182).
logger = logging.getLogger("inflow_lab")


@dataclass(slots=True)
class DeliveryOutcome:
    response: dict[str, Any] | None = None
    delivery_status: str = "generated"
    posted: int = 0
    failed: int = 0
    delivery_attempts: int = 0
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None


class SimulationController:
    def __init__(
        self,
        engine: LegitFlowEngine,
        store: EventStore,
        scenario_saves: ScenarioSaveStore,
        mock_service: MockRegEngineService,
        live_client: LiveRegEngineClient,
    ) -> None:
        self.engine = engine
        self.store = store
        self.scenario_saves = scenario_saves
        self.mock_service = mock_service
        self.live_client = live_client
        self.config = SimulationConfig(persist_path=str(store.persist_path))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        # Guards ONLY _task/_stop_event, and is never held across a network
        # await (#158). self._lock is the data-plane lock: every delivery path
        # awaits HTTP inside it, and #103's chunking turned one lock-held call
        # into ceil(N/500) sequential ones. If stop() had to take self._lock
        # just to signal, a large replay could make it unreachable for
        # minutes -- so stop() takes this lock instead and can always signal
        # immediately, which is what #158 asks for.
        #
        # Lock ordering, to keep this deadlock-free: self._lock -> this one,
        # never the reverse. start() holds self._lock and then briefly takes
        # this; stop() takes ONLY this and never self._lock. Since no path
        # acquires self._lock while holding this, there is no cycle.
        self._lifecycle_lock = asyncio.Lock()
        self._revision = 0
        self._change_condition = asyncio.Condition()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def revision(self) -> int:
        return self._revision

    async def start(self, config: SimulationConfig) -> None:
        async with self._lock:
            _validate_live_delivery(config.delivery)
            previous_config = self.config
            if self.running and (
                _engine_shaping_fields_changed(previous_config, config)
                or config.persist_path != previous_config.persist_path
            ):
                # #96: a running engine keeps generating events from
                # previous_config's scenario/seed/scale no matter what
                # self.config is reassigned to -- reset() is the only thing
                # that actually changes what the engine produces, and below
                # it only runs `if not self.running`. Applying the new
                # config here anyway (as this method used to, unconditionally)
                # left status() self-contradicting: config.scenario reports
                # the new request while stats.engine.scenario -- and every
                # event actually being emitted -- stays on the old one. A
                # differing persist_path has its own version of the same
                # problem: EventStore.configure() both repoints persist_path
                # and reloads the in-memory ring from *that* file, so the
                # still-running loop would keep writing this one run's
                # events split across the old and new JSONL files.
                #
                # The console itself already treats "running" and "start" as
                # mutually exclusive -- syncRunButtons() in
                # app/static/app.js disables the Start button for the
                # entire time status.running is true, and only re-enables it
                # once Stop has completed -- so an operator can never reach
                # this branch through the UI. Only a direct API call, or a
                # second tab whose own "running" state has gone stale, can.
                # A 409 tells that caller the same thing the UI already
                # enforces by disabling the button, instead of silently
                # discarding either the running engine's state or the
                # caller's own request.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Simulation is already running with a different scenario, "
                        "seed, scale, or persist_path. POST /api/simulate/stop first, "
                        "then start with the new config."
                    ),
                )
            self.config = config
            await self._store_configure(config.persist_path)
            if not self.running and _engine_shaping_fields_changed(previous_config, config):
                self.engine.reset(config.seed, scenario=config.scenario, scale=config.scale)
            if not self.running:
                # Both fields assigned together under _lifecycle_lock, so
                # stop() always reads a matched generation (#156). Held only
                # across the assignment and create_task, never an await of
                # real work.
                async with self._lifecycle_lock:
                    stop_event = asyncio.Event()
                    self._stop_event = stop_event
                    self._task = asyncio.create_task(self._run_loop(stop_event))
        await self._publish_update()

    async def stop(self) -> None:
        # #156: the _task/_stop_event mutation below must happen under
        # _lifecycle_lock -- the same lock start() assigns them under -- or a
        # concurrent
        # start() can land in the gap this used to leave open. The old
        # body read self.running, set self._stop_event, and unconditionally
        # cleared self._task with no lock at all, so a start() landing
        # between "the awaited task finished" and "this coroutine resumed"
        # would install its own task/event and then have both silently
        # discarded by this method's own `self._task = None` -- orphaning
        # a live run loop that nothing could ever reach again.
        async with self._lifecycle_lock:
            task = self._task
            if task is None or task.done():
                return
            # Captured under the same lock as `task`, from the same read of
            # self._task's generation -- start() always assigns
            # self._stop_event and self._task together (see above), so this
            # is guaranteed to be the event *this* task is actually waiting
            # on, never a later start()'s fresh replacement.
            stop_event = self._stop_event
            stop_event.set()
        # await the task OUTSIDE the lock. _run_loop calls self.step(),
        # which itself does `async with self._lock` -- holding the lock
        # here while awaiting the loop's own task would deadlock stop()
        # against itself the instant the loop is mid-step (or about to
        # start its next one): step() would block forever on a lock stop()
        # is holding, while stop() blocks forever on a task that can now
        # never finish stepping.
        await task
        async with self._lifecycle_lock:
            # The other half of #156's fix. While the lock was open across
            # the `await task` above, a concurrent start() could have
            # observed `running is False` (this task had just finished) and
            # installed a fresh task + event of its own -- exactly the race
            # this issue is about. Only clear self._task if it is still
            # this exact task object: if start() already replaced it,
            # self._task now names a different, live run loop, and clearing
            # it here would orphan that loop the same way the original bug
            # did. A mismatch makes this call a no-op for that new loop --
            # left running and referenced, for a later stop() to reach.
            if self._task is not task:
                return
            self._task = None
        await self._publish_update()

    async def shutdown(self) -> None:
        await self.stop()

    async def reset(self, config: SimulationConfig | None = None) -> None:
        await self.stop()
        async with self._lock:
            if config is not None:
                self.config = config
            await self._store_configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
            self.store.reset()
            self.mock_service.reset()
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        async with self._lock:
            _validate_live_delivery(self.config.delivery)
            size = batch_size or self.config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

            payload = IngestPayload(source=self.config.source, events=events)
            outcome = await self._deliver_payload(
                payload,
                self.config,
                idempotency_key=uuid.uuid4().hex,
            )

            stored_records = _build_stored_records(
                self.config.source, events, lineages, self.config.delivery.mode, outcome
            )
            await self._store_add_many(stored_records)

            result = StepResponse(
                generated=len(events),
                posted=outcome.posted,
                accepted=(outcome.response or {}).get("accepted", 0),
                rejected=(outcome.response or {}).get("rejected", 0),
                failed=outcome.failed,
                lot_codes=[event.traceability_lot_code for event in events],
                delivery_status=outcome.delivery_status,
                delivery_mode=self.config.delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                response=outcome.response,
                error=outcome.error_message,
            )
        await self._publish_update()
        return result

    async def replay(self, request: ReplayRequest | None = None) -> ReplayResponse:
        request = request or ReplayRequest()
        async with self._lock:
            persist_path = request.persist_path or self.config.persist_path
            source = request.source or self.config.source
            delivery = request.delivery or self.config.delivery
            _validate_live_delivery(delivery)
            replay_config = self.config.model_copy(
                update={
                    "source": source,
                    "delivery": delivery,
                },
                deep=True,
            )
            records = await self._store_read_persisted_records(persist_path)
            events = [record.event for record in records]

            if not events:
                result = ReplayResponse(
                    status="empty",
                    read=0,
                    replayed=0,
                    posted=0,
                    failed=0,
                    source=source,
                    persist_path=persist_path,
                    delivery_mode=delivery.mode,
                    delivery_attempts=0,
                )
            else:
                # #103: RegEngine caps a batch at MAX_BATCH_EVENTS events and
                # 422s the whole request past that -- a persisted store
                # replayed here has no such bound, so once it grows past the
                # cap a bare single _deliver_payload call would fail the
                # entire replay every time (permanently, since the store
                # only grows). _deliver_in_chunks splits `events` into
                # slices at that same cap and posts each as its own
                # request; _aggregate_outcomes rolls the per-chunk results
                # back into one summary so an early chunk's posted events
                # are still reported even if a later chunk fails.
                chunks = await self._deliver_in_chunks(source, events, replay_config)
                outcome = _aggregate_outcomes([chunk_outcome for _, chunk_outcome in chunks])
                replay_status = {
                    "posted": "posted",
                    "failed": "failed",
                    "generated": "rebuilt",
                }[outcome.delivery_status]
                result = ReplayResponse(
                    status=replay_status,
                    read=len(records),
                    replayed=len(events),
                    posted=outcome.posted,
                    failed=outcome.failed,
                    source=source,
                    persist_path=persist_path,
                    delivery_mode=delivery.mode,
                    delivery_attempts=outcome.delivery_attempts,
                    response=outcome.response,
                    error=outcome.error_message,
                )
        await self._publish_update()
        return result

    async def import_csv(self, request: CSVImportRequest) -> CSVImportResponse:
        async with self._lock:
            source = request.source or self.config.source
            delivery = request.delivery or self.config.delivery
            _validate_live_delivery(delivery)
            import_config = self.config.model_copy(
                update={
                    "source": source,
                    "delivery": delivery,
                },
                deep=True,
            )
            # CPU-bound parsing (#136): offload it too, same reasoning as
            # the store calls above -- a large csv_text body would
            # otherwise tie up the event loop for the whole parse before
            # the store write even starts.
            parsed = await asyncio.to_thread(
                parse_csv_import,
                request.import_type,
                request.csv_text,
                default_timestamp=datetime.now(UTC),
            )

            outcome = DeliveryOutcome()
            stored_records: list[StoredEventRecord] = []
            if parsed.events:
                # #103: same MAX_BATCH_EVENTS cap as replay() -- a CSV
                # import has no row-count bound of its own, so a batch
                # parsed from a large file used to be posted as one
                # oversized request and 422 in full. Chunk here and build
                # stored records per chunk (each chunk keeps its own
                # outcome, so a record's delivery_attempts reflects the one
                # real POST it was actually part of, not the whole
                # import's chunk count).
                chunks = await self._deliver_in_chunks(source, parsed.events, import_config)
                lineage_offset = 0
                for chunk_events, chunk_outcome in chunks:
                    chunk_len = len(chunk_events)
                    stored_records.extend(
                        _build_stored_records(
                            source,
                            chunk_events,
                            parsed.parent_lot_codes[lineage_offset : lineage_offset + chunk_len],
                            delivery.mode,
                            chunk_outcome,
                        )
                    )
                    lineage_offset += chunk_len
                await self._store_add_many(stored_records)
                # Response-level counts only, aggregated across every chunk
                # (#103) -- per-record fields above already used each
                # chunk's own outcome instead.
                outcome = _aggregate_outcomes([chunk_outcome for _, chunk_outcome in chunks])

            rejected = parsed.total - len(parsed.events)
            if outcome.delivery_status == "failed":
                status = "delivery_failed"
            elif parsed.events and parsed.errors:
                status = "partial"
            elif parsed.events:
                status = "accepted"
            else:
                status = "rejected"

            result = CSVImportResponse(
                status=status,
                import_type=request.import_type,
                total=parsed.total,
                accepted=len(parsed.events),
                rejected=rejected,
                stored=len(stored_records),
                posted=outcome.posted,
                failed=outcome.failed,
                source=source,
                delivery_mode=delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                lot_codes=[event.traceability_lot_code for event in parsed.events],
                errors=parsed.errors,
                warnings=parsed.warnings,
                response=outcome.response,
                error=outcome.error_message,
            )
        await self._publish_update()
        return result

    async def load_demo_fixture(
        self,
        fixture_id: DemoFixtureId,
        request: DemoFixtureLoadRequest | None = None,
    ) -> DemoFixtureLoadResponse:
        request = request or DemoFixtureLoadRequest()
        fixture = get_demo_fixture(fixture_id)
        await self.stop()

        async with self._lock:
            source = request.source or self.config.source
            delivery = request.delivery or self.config.delivery
            _validate_live_delivery(delivery)
            self.config = self.config.model_copy(
                update={
                    "source": source,
                    "scenario": fixture.scenario,
                    "delivery": delivery,
                },
                deep=True,
            )
            await self._store_configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=fixture.scenario, scale=self.config.scale)
            if request.reset:
                self.store.reset()
                self.mock_service.reset()

            events = [fixture_event.event for fixture_event in fixture.events]
            payload = IngestPayload(source=source, events=events)
            outcome = await self._deliver_payload(payload, self.config)
            fixture_parent_lot_codes = [
                list(fixture_event.parent_lot_codes) for fixture_event in fixture.events
            ]
            stored_records = _build_stored_records(
                source, events, fixture_parent_lot_codes, delivery.mode, outcome
            )
            await self._store_add_many(stored_records)
            result = DemoFixtureLoadResponse(
                status="delivery_failed" if outcome.delivery_status == "failed" else "loaded",
                fixture_id=fixture.id,
                scenario=fixture.scenario,
                loaded=len(events),
                stored=len(stored_records),
                posted=outcome.posted,
                failed=outcome.failed,
                source=source,
                delivery_mode=delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                lot_codes=fixture.lot_codes,
                response=outcome.response,
                error=outcome.error_message,
            )
        await self._publish_update()
        return result

    def list_scenario_saves(self) -> ScenarioSaveListResponse:
        return ScenarioSaveListResponse(
            saves=[
                self._scenario_save_summary(snapshot)
                for snapshot in self.scenario_saves.list()
            ]
        )

    async def save_scenario(
        self,
        scenario_id: ScenarioId,
        request: ScenarioSaveRequest | None = None,
    ) -> ScenarioSaveResponse:
        request = request or ScenarioSaveRequest()
        async with self._lock:
            config = request.config or self.config
            config = config.model_copy(update={"scenario": scenario_id}, deep=True)
            config = self._sanitize_saved_config(config)
            records = self.store.all_between()
            snapshot = self.scenario_saves.save_snapshot(
                scenario=scenario_id,
                config=config,
                records=records,
            )
            result = ScenarioSaveResponse(
                status="saved",
                save=self._scenario_save_summary(snapshot),
                config=snapshot.config,
            )
        return result

    async def load_scenario_save(self, scenario_id: ScenarioId) -> ScenarioLoadResponse:
        await self.stop()
        snapshot = self.scenario_saves.get(scenario_id)
        if snapshot is None:
            raise KeyError(scenario_id.value)

        async with self._lock:
            self.config = snapshot.config
            await self._store_configure(self.config.persist_path)
            loaded_records = await self._store_replace_all(snapshot.records)
            self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
            result = ScenarioLoadResponse(
                status="loaded",
                save=self._scenario_save_summary(snapshot),
                config=self.config,
                loaded_records=len(loaded_records),
            )
        await self._publish_update()
        return result

    async def retry_failed_delivery(
        self,
        request: DeliveryRetryRequest | None = None,
    ) -> DeliveryRetryResponse:
        request = request or DeliveryRetryRequest()
        async with self._lock:
            delivery = request.delivery or self.config.delivery
            # record_ids distinguishes "omitted" (None -- no filter, retry
            # every failed record up to limit) from "present but empty"
            # ([] -- retry nothing). EventStore.failed_delivery_records()
            # cannot make that distinction itself: it builds its filter as
            # set(record_ids or []), which treats [] exactly like None and
            # so retries everything for an explicitly empty list (#144).
            # Short-circuiting the empty-list case here, before the store
            # is ever consulted, fixes the caller-visible behavior without
            # touching that store method's filtering logic.
            if request.record_ids is not None and len(request.record_ids) == 0:
                candidates: list[StoredEventRecord] = []
            else:
                candidates = self.store.failed_delivery_records(request.record_ids, limit=request.limit)
            # Truthiness (`if request.record_ids`) would misreport this the
            # same way: an explicitly empty list is falsy just like None, so
            # `requested` must key off "was the field provided at all" too.
            requested = len(request.record_ids) if request.record_ids is not None else len(candidates)
            skipped = max(0, requested - len(candidates))

            if not candidates:
                result = DeliveryRetryResponse(
                    status="empty",
                    requested=requested,
                    retryable=0,
                    attempted=0,
                    posted=0,
                    failed=0,
                    skipped=skipped,
                    delivery_mode=delivery.mode,
                    record_ids=[],
                )
            elif delivery.mode == DestinationMode.NONE:
                result = DeliveryRetryResponse(
                    status="skipped",
                    requested=requested,
                    retryable=len(candidates),
                    attempted=0,
                    posted=0,
                    failed=0,
                    skipped=skipped + len(candidates),
                    delivery_mode=delivery.mode,
                    record_ids=[record.record_id for record in candidates],
                    error="Retry requires mock or live delivery mode.",
                )
            else:
                _validate_live_delivery(delivery)
                posted = 0
                failed = 0
                updated_records: list[StoredEventRecord] = []
                responses: list[dict[str, Any]] = []
                grouped_records: dict[tuple[str, str | None], list[StoredEventRecord]] = {}
                for record in candidates:
                    source = request.source or record.payload_source
                    idempotency_key = (
                        _stored_idempotency_key(record)
                        if delivery.mode != DestinationMode.NONE
                        else None
                    )
                    grouped_records.setdefault((source, idempotency_key), []).append(record)

                for (source, idempotency_key), records in grouped_records.items():
                    retry_config = self.config.model_copy(
                        update={
                            "source": source,
                            "delivery": delivery,
                        },
                        deep=True,
                    )
                    payload = IngestPayload(source=source, events=[record.event for record in records])
                    # #95: the stored key is only safe to reuse when the
                    # original attempt produced no response at all --
                    # record.delivery_response is None -- a pure transport
                    # failure, where retrying under the same key is a
                    # legitimate "did this already land" idempotency check.
                    # A record carrying a per-event verdict (accepted or
                    # rejected) already has an authoritative response on
                    # file; reusing its key would have RegEngine's
                    # idempotency cache answer this retry from that stale
                    # response without ever revalidating the corrected
                    # event -- exactly the bug #95 reports. `all(...)` over
                    # the whole group, not a per-record check: every record
                    # here shares this one original idempotency_key (that's
                    # what grouped them together above), so if even one of
                    # them did get a response, that key's original attempt
                    # was not "no response at all" and nothing in the group
                    # is safe to replay under it. Passing None makes
                    # _deliver_payload mint a fresh key, same as a record
                    # with no stored key at all already got before this fix.
                    reuse_original_key = idempotency_key is not None and all(
                        record.delivery_response is None for record in records
                    )
                    outcome = await self._deliver_payload(
                        payload,
                        retry_config,
                        idempotency_key=idempotency_key if reuse_original_key else None,
                    )
                    response_events = (outcome.response or {}).get("events", []) if outcome.response else []
                    responses.append(
                        {
                            "source": source,
                            "delivery_status": outcome.delivery_status,
                            "posted": outcome.posted,
                            "failed": outcome.failed,
                            "response": outcome.response,
                            "error": outcome.error_message,
                        }
                    )

                    paired_responses = _pair_event_responses(
                        [record.event for record in records], response_events
                    )
                    for index, record in enumerate(records):
                        event_response = paired_responses[index]
                        next_attempts = record.delivery_attempts + outcome.delivery_attempts
                        fields = _event_delivery_fields(outcome, event_response)
                        if fields["delivery_status"] == "posted":
                            fields["error"] = None
                        else:
                            fields["last_delivery_success_at"] = record.last_delivery_success_at
                        updated_records.append(
                            record.model_copy(
                                update={
                                    "destination_mode": delivery.mode,
                                    "delivery_attempts": next_attempts,
                                    "last_delivery_attempt_at": outcome.attempted_at
                                    or record.last_delivery_attempt_at,
                                    "delivery_response": event_response,
                                    "delivery_metadata": outcome.metadata,
                                    **fields,
                                },
                                deep=True,
                            )
                        )
                    posted += outcome.posted
                    failed += outcome.failed

                await self._store_update_many(updated_records)
                if posted and failed:
                    status = "partial"
                elif failed:
                    status = "failed"
                else:
                    status = "posted"
                result = DeliveryRetryResponse(
                    status=status,
                    requested=requested,
                    retryable=len(candidates),
                    attempted=len(candidates),
                    posted=posted,
                    failed=failed,
                    skipped=skipped,
                    delivery_mode=delivery.mode,
                    record_ids=[record.record_id for record in candidates],
                    responses=responses,
                    error=next((item["error"] for item in responses if item.get("error")), None),
                )
        await self._publish_update()
        return result

    # -- Running EventStore's blocking calls off the event loop (#136) -----
    #
    # EventStore guards its state with a threading.RLock (see the comment
    # on that lock in app/store.py) and does plain synchronous
    # open()/write()/replace() calls. Called directly from one of the
    # `async def` methods above, that I/O would run on the single event
    # loop thread and block every other request in this process -- other
    # tenants' SSE streams, health checks, unrelated API calls -- for as
    # long as a big CSV import, delivery retry, or replay takes.
    #
    # The methods below are the only place offloading happens. Each one
    # runs exactly one EventStore call, start to finish, on a worker
    # thread via asyncio.to_thread, then returns its result (or re-raises
    # its exception) to the caller, so e.g. ``await
    # self._store_add_many(...)`` behaves like ``self.store.add_many(...)``
    # in every observable way except which thread does the work, and that
    # the event loop stays free to run other coroutines while it waits.
    # EventStore's own methods are untouched and still fully synchronous --
    # deliberately, since read_persisted_records() in particular is also
    # called from plain sync code with no event loop at all
    # (EventStore.__init__ and MockRegEngineService.__init__).
    #
    # Why this is safe for the RLock specifically: every EventStore method
    # wrapped below is a plain `def`, never `async def`, so it acquires
    # and fully releases the RLock within one synchronous call frame --
    # there is no `await` anywhere inside the locked section for it to
    # suspend on. asyncio.to_thread hands that whole frame to one worker
    # thread and waits for it to finish before this coroutine resumes, so:
    #   - the lock still serializes concurrent callers correctly, exactly
    #     as it does today, because RLock provides mutual exclusion across
    #     *any* threads asking for it, not only reentrancy within one --
    #     it does not matter that the thread asking now is a worker thread
    #     instead of the event loop's own thread;
    #   - the reentrant re-acquisitions inside the store (configure ->
    #     _load_from_disk -> read_persisted_records; update_many ->
    #     _all_records -> read_persisted_records) stay on that same single
    #     worker thread from start to finish, so RLock's "reentrant only
    #     for the thread that already holds it" guarantee is never asked
    #     to span two different threads, which is the one thing that would
    #     actually break it.
    # Do not turn EventStore's add_many/update_many/replace_all/configure/
    # read_persisted_records into `async def` methods that hold its lock
    # across an `await` -- that would let the event loop thread block
    # waiting on a worker thread for the lock while that worker is itself
    # waiting on the event loop to resume it: a deadlock. Keep EventStore
    # synchronous and do the `asyncio.to_thread` here, at the call site.
    #
    # Separately: self._lock above (SimulationController's own lock) is an
    # asyncio.Lock, not a threading lock, and EventStore never touches it.
    # Holding it across one of these awaits, as step/import_csv/replay/etc.
    # do, is ordinary cooperative-lock usage with no OS thread pinned to
    # it, so it cannot deadlock against the RLock inside asyncio.to_thread
    # -- there is no cycle where one waits on the other.
    async def _store_add_many(
        self, records: Iterable[StoredEventRecord]
    ) -> list[StoredEventRecord]:
        return await asyncio.to_thread(self.store.add_many, records)

    async def _store_update_many(
        self, records: Iterable[StoredEventRecord]
    ) -> list[StoredEventRecord]:
        return await asyncio.to_thread(self.store.update_many, records)

    async def _store_replace_all(
        self, records: Iterable[StoredEventRecord]
    ) -> list[StoredEventRecord]:
        return await asyncio.to_thread(self.store.replace_all, records)

    async def _store_read_persisted_records(
        self, persist_path: str | None = None
    ) -> list[StoredEventRecord]:
        return await asyncio.to_thread(self.store.read_persisted_records, persist_path)

    async def _store_configure(self, persist_path: str) -> None:
        await asyncio.to_thread(self.store.configure, persist_path)

    async def _deliver_payload(
        self,
        payload: IngestPayload,
        config: SimulationConfig,
        idempotency_key: str | None = None,
    ) -> DeliveryOutcome:
        if config.delivery.mode == DestinationMode.NONE:
            return DeliveryOutcome()

        api_key = config.delivery.api_key
        attempted_at = datetime.now(UTC)
        delivery_idempotency_key = idempotency_key
        if config.delivery.mode != DestinationMode.NONE and delivery_idempotency_key is None:
            delivery_idempotency_key = uuid.uuid4().hex
        try:
            if config.delivery.mode == DestinationMode.MOCK:
                response = self.mock_service.ingest(
                    payload,
                    idempotency_key=delivery_idempotency_key,
                    friction=tuple(config.delivery.mock_friction),
                ).model_dump(mode="json")
                metadata = {
                    "delivery_mode": "mock",
                    "attempted_event_count": len(payload.events),
                    "idempotency_key": delivery_idempotency_key,
                }
            elif config.delivery.mode == DestinationMode.LIVE:
                result = await self.live_client.ingest(
                    payload,
                    config,
                    idempotency_key=delivery_idempotency_key,
                )
                response = result.response
                metadata = result.metadata | {"attempted_event_count": len(payload.events)}
            else:
                return DeliveryOutcome()

            # #95: a repeated Idempotency-Key makes both the mock and live
            # RegEngine replay a cached response verbatim, without
            # revalidating anything (app/mock_service.py's
            # _idempotency_cache; live RegEngine's own idempotency store
            # behaves the same, per the shared TTL cache both sides pin
            # against). That is correct when the replayed request is the
            # one that populated the cache entry -- but retry_failed_delivery
            # can legitimately resend a *subset* of a larger original batch
            # under that batch's key (see its reuse_original_key comment
            # above), so the cached response can describe more events than
            # this request actually sent. Every genuine (non-replayed)
            # response carries exactly one verdict per requested event --
            # the same invariant _pair_event_responses relies on -- so a
            # mismatched count is the tell. Treat that as a replay rather
            # than a delivery: folding a stale batch's accepted/rejected
            # counts into this request's posted/failed would silently
            # overstate what this retry actually accomplished (#95's
            # second bug).
            masked_response = mask_secret_in_payload(response, api_key)
            if _is_idempotency_replay(response, payload):
                return DeliveryOutcome(
                    response=masked_response,
                    delivery_status="failed",
                    posted=0,
                    failed=len(payload.events),
                    delivery_attempts=1,
                    attempted_at=attempted_at,
                    completed_at=datetime.now(UTC),
                    error_message=(
                        "RegEngine replayed a cached idempotency response sized "
                        "for a different request; none of this request's events "
                        "were revalidated. Retry again to mint a fresh key."
                    ),
                    metadata=metadata | {"idempotency_replay": True},
                )
            return DeliveryOutcome(
                response=masked_response,
                delivery_status="posted",
                posted=response.get("accepted", len(payload.events)),
                failed=response.get("rejected", 0),
                delivery_attempts=1,
                attempted_at=attempted_at,
                completed_at=datetime.now(UTC),
                metadata=metadata,
            )
        except MockRegEngineHTTPError as exc:
            logger.error(
                "mock delivery failed: %s events rejected with HTTP %s (%s)",
                len(payload.events),
                exc.status_code,
                exc,
            )
            return DeliveryOutcome(
                delivery_status="failed",
                failed=len(payload.events),
                delivery_attempts=1,
                attempted_at=attempted_at,
                error_message=str(exc),
                metadata={
                    "delivery_mode": "mock",
                    "attempted_event_count": len(payload.events),
                    "status_code": exc.status_code,
                    "idempotency_key": delivery_idempotency_key,
                },
            )
        except LiveRegEngineDeliveryError as exc:
            # Masked: the raw exception can quote the request, and the API key
            # rides in a header. Host, not full URL, for the same reason.
            logger.error(
                "live delivery to %s failed: %s events not posted (%s)",
                urlparse(str(config.delivery.endpoint or "")).hostname or "unknown host",
                len(payload.events),
                mask_secret_in_string(str(exc), api_key),
            )
            metadata = exc.metadata | {"attempted_event_count": len(payload.events)}
            if delivery_idempotency_key and "idempotency_key" not in metadata:
                metadata["idempotency_key"] = delivery_idempotency_key
            return DeliveryOutcome(
                delivery_status="failed",
                failed=len(payload.events),
                delivery_attempts=1,
                attempted_at=attempted_at,
                error_message=mask_secret_in_string(str(exc), api_key),
                metadata=metadata,
            )
        except Exception as exc:  # pragma: no cover - exercised by live integration, not unit tests
            logger.exception(
                "unexpected %s delivery failure: %s events not posted (%s)",
                config.delivery.mode.value,
                len(payload.events),
                mask_secret_in_string(str(exc), api_key),
            )
            metadata = {
                "delivery_mode": config.delivery.mode.value,
                "attempted_event_count": len(payload.events),
            }
            if delivery_idempotency_key:
                metadata["idempotency_key"] = delivery_idempotency_key
            return DeliveryOutcome(
                delivery_status="failed",
                failed=len(payload.events),
                delivery_attempts=1,
                attempted_at=attempted_at,
                error_message=mask_secret_in_string(str(exc), api_key),
                metadata=metadata,
            )

    async def _deliver_in_chunks(
        self,
        source: str,
        events: list[RegEngineEvent],
        config: SimulationConfig,
    ) -> list[tuple[list[RegEngineEvent], DeliveryOutcome]]:
        """POST *events* to RegEngine in slices of at most MAX_BATCH_EVENTS (#103).

        RegEngine's real webhook route -- and the mock mirroring it, see
        app/mock_service.py's MAX_BATCH_EVENTS -- rejects the entire
        request with one 422 the instant a batch exceeds this cap.
        import_csv and replay are the two callers that can hand this more
        events than that: neither bounds how many CSV rows get parsed or
        how large a persisted store has grown before building one
        IngestPayload (step() cannot -- batch_size caps at 100 -- and
        retry_failed_delivery cannot either, since DeliveryRetryRequest.limit
        caps at 500, this same constant's value).

        Each chunk goes out as its own request via _deliver_payload, which
        mints it a fresh Idempotency-Key exactly as it already does for any
        caller that passes none -- from RegEngine's side a chunk is a
        wholly separate request, never a retry of another one.

        Returns each chunk's own event slice paired with its own
        DeliveryOutcome, in order, so a caller that must pair per-event
        verdicts back onto records (import_csv, via _build_stored_records)
        can zip each outcome against exactly the events that produced it
        instead of re-deriving chunk boundaries a second time. A call with
        `events` at or under the cap produces exactly one chunk covering
        the whole list -- the common case, and _aggregate_outcomes passes
        a single outcome straight through unchanged, so it behaves
        identically to the unchunked call this replaces.
        """
        chunks: list[tuple[list[RegEngineEvent], DeliveryOutcome]] = []
        for start in range(0, len(events), MAX_BATCH_EVENTS):
            chunk_events = events[start : start + MAX_BATCH_EVENTS]
            payload = IngestPayload(source=source, events=chunk_events)
            outcome = await self._deliver_payload(payload, config)
            chunks.append((chunk_events, outcome))
        return chunks

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        # Takes stop_event as a parameter, captured once, rather than
        # re-reading self._stop_event on every iteration (#156, trap 2).
        # start() assigns a fresh asyncio.Event to self._stop_event every
        # time it launches a new loop; a loop that instead kept reading
        # that attribute could have its stop signal swapped out from under
        # it -- if this loop is still draining (finishing a step, or about
        # to re-check this condition) when a *new* start() installs its own
        # fresh, unset event, self._stop_event would no longer be the event
        # this loop was actually told to stop with, and it would see
        # "not asked to stop" again and keep going, resurrected by a
        # start() call that has nothing to do with it. Capturing the one
        # event this loop was created with, once, makes it deaf to any
        # later generation's event by construction.
        while not stop_event.is_set():
            await self.step(self.config.batch_size)
            if self.config.interval_seconds <= 0:
                await asyncio.sleep(0)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.config.interval_seconds)
                except asyncio.TimeoutError:
                    continue

    async def _publish_update(self) -> None:
        async with self._change_condition:
            self._revision += 1
            self._change_condition.notify_all()

    async def wait_for_revision(self, revision: int, timeout: float = 15.0) -> int:
        async with self._change_condition:
            await asyncio.wait_for(
                self._change_condition.wait_for(lambda: self._revision != revision),
                timeout=timeout,
            )
            return self._revision

    def snapshot(self, event_limit: int = 100) -> dict[str, Any]:
        return {
            "revision": self._revision,
            "status": self.status(),
            "events": [record.model_dump(mode="json") for record in self.store.recent(limit=event_limit)],
        }

    def status(self) -> dict[str, Any]:
        records = self.store.all_between()
        scenario = get_scenario(self.config.scenario)
        return {
            "running": self.running,
            "config": self._sanitize_public_config(self.config).model_dump(mode="json"),
            "stats": {
                **self.store.stats(),
                "audit": summarize_scenario_audit(records, scenario),
                "engine": self.engine.snapshot(),
            },
        }

    def integration_status(self) -> IntegrationStatusResponse:
        delivery = self.config.delivery
        endpoint = str(delivery.endpoint) if delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
        return IntegrationStatusResponse(
            mode=delivery.mode,
            endpoint=endpoint,
            endpoint_host=urlparse(endpoint).netloc,
            api_key_configured=bool(delivery.api_key),
            tenant_configured=bool(delivery.tenant_id),
            hmac_configured=bool(os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip()),
            contract_version=INFLOW_CONTRACT_VERSION,
            mock_friction=list(delivery.mock_friction),
        )

    async def configure_integration(
        self, request: IntegrationConfigureRequest
    ) -> IntegrationStatusResponse:
        async with self._lock:
            updates: dict[str, Any] = {}
            if request.mode is not None:
                updates["mode"] = request.mode
            if request.endpoint is not None:
                updates["endpoint"] = request.endpoint
            if request.api_key is not None:
                updates["api_key"] = request.api_key or None
            if request.tenant_id is not None:
                updates["tenant_id"] = request.tenant_id or None
            if request.mock_friction is not None:
                updates["mock_friction"] = list(request.mock_friction)
            delivery = self.config.delivery.model_copy(update=updates, deep=True)
            _validate_live_delivery(delivery)
            self.config = self.config.model_copy(update={"delivery": delivery}, deep=True)
        await self._publish_update()
        return self.integration_status()

    def _sanitize_public_config(self, config: SimulationConfig) -> SimulationConfig:
        delivery = config.delivery.model_copy(update={"api_key": None}, deep=True)
        if delivery.mode == DestinationMode.LIVE:
            delivery = delivery.model_copy(update={"tenant_id": None}, deep=True)
        return config.model_copy(update={"delivery": delivery}, deep=True)

    def _sanitize_saved_config(self, config: SimulationConfig) -> SimulationConfig:
        delivery = config.delivery.model_copy(update={"api_key": None}, deep=True)
        if delivery.mode == DestinationMode.LIVE:
            delivery = delivery.model_copy(
                update={
                    "mode": DestinationMode.MOCK,
                    "endpoint": None,
                    "tenant_id": None,
                    "api_key": None,
                },
                deep=True,
            )
        return config.model_copy(update={"delivery": delivery}, deep=True)

    def _scenario_save_summary(self, snapshot: ScenarioSaveSnapshot) -> ScenarioSaveSummary:
        lot_codes = []
        for record in sorted(snapshot.records, key=lambda item: item.sequence_no):
            lot_code = record.event.traceability_lot_code
            if lot_code not in lot_codes:
                lot_codes.append(lot_code)
        scenario = get_scenario(snapshot.scenario)
        return ScenarioSaveSummary(
            scenario=snapshot.scenario,
            label=scenario.label,
            saved_at=snapshot.saved_at,
            record_count=len(snapshot.records),
            lot_codes=lot_codes,
            source=snapshot.config.source,
            persist_path=snapshot.config.persist_path,
            delivery_mode=snapshot.config.delivery.mode,
        )


def _pair_event_responses(
    events: list[Any],
    response_events: list[Any],
) -> list[dict[str, Any] | None]:
    """Match each sent event to its own verdict, keyed on identity.

    RegEngine does not answer in request order. Its webhook route builds
    the response in phases — replay-window and KDE rejections first, then
    rules-enforcement rejections, then every acceptance — so the response
    list is partitioned rejected-first. Zipping it against the request by
    array index therefore attaches one event's verdict to a different
    event whenever a rejection follows an acceptance: a valid lot is
    stored as ``failed`` carrying another lot's validation errors, and the
    ``event_id``/``sha256_hash``/``chain_hash`` of a real accepted event
    are copied onto the wrong record. Those hashes are the evidence.

    ``(traceability_lot_code, cte_type)`` is on the wire on both sides and
    is the join key. A key can legitimately repeat inside one batch — the
    same lot and CTE at two different timestamps — so responses are held
    in a per-key queue and consumed in order rather than collapsed into a
    dict. Within a partition RegEngine preserves request order, so the
    Nth event with a given key gets the Nth response with that key.

    A key with no response left is ``None``: no verdict. Callers must
    treat that as "unknown", never fall back to positional matching —
    a wrong verdict is worse than an absent one.

    Note this is invisible to the unit suite by construction:
    ``mock_service`` appends accepted and rejected in a single pass, so
    the mock's response *is* in request order.
    """
    by_key: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for response in response_events:
        if not isinstance(response, dict):
            continue
        lot_code = response.get("traceability_lot_code")
        cte_type = response.get("cte_type")
        if lot_code is None or cte_type is None:
            continue
        by_key[(str(lot_code), str(cte_type))].append(response)

    paired: list[dict[str, Any] | None] = []
    for event in events:
        cte_type = getattr(event.cte_type, "value", event.cte_type)
        queue = by_key.get((str(event.traceability_lot_code), str(cte_type)))
        paired.append(queue.popleft() if queue else None)
    return paired

def _event_delivery_fields(outcome: DeliveryOutcome, event_response: dict[str, Any] | None) -> dict[str, Any]:
    """Per-record delivery fields honoring per-event rejection.

    RegEngine (and the mock mirroring it) returns HTTP 2xx with per-event
    accepted/rejected statuses; a rejected event was not ingested, so its
    stored record must read as failed with the validator's errors.
    """
    status = outcome.delivery_status
    error = outcome.error_message
    if (
        status == "posted"
        and isinstance(event_response, dict)
        and event_response.get("status") == "rejected"
    ):
        status = "failed"
        errors = event_response.get("errors") or []
        error = "; ".join(str(item) for item in errors) or "Event rejected by ingest validation"
    return {
        "delivery_status": status,
        "last_delivery_success_at": outcome.completed_at if status == "posted" else None,
        "error": error,
    }


def _build_stored_records(
    source: str,
    events: list[RegEngineEvent],
    parent_lot_codes: list[list[str]],
    delivery_mode: DestinationMode,
    outcome: DeliveryOutcome,
) -> list[StoredEventRecord]:
    """Build one fresh ``StoredEventRecord`` per event from one ``DeliveryOutcome`` (#165).

    ``step``, ``import_csv``, and ``load_demo_fixture`` each post a freshly
    generated batch of events through a single ``DeliveryOutcome`` and need
    the same event-to-response pairing (``_pair_event_responses``) plus
    per-event field derivation (``_event_delivery_fields``) before
    persisting -- this was copy-pasted three times with only the
    source/events/parent_lot_codes/delivery_mode inputs differing per call
    site, risking silent drift between the copies.

    ``events[i]`` and ``parent_lot_codes[i]`` must correspond to the same
    event -- callers zip their own per-event lineage against their own
    event list before calling in, since where that lineage comes from
    (engine.next_event, CSV parsing, a demo fixture) differs per call site.

    ``retry_failed_delivery`` does NOT go through this helper: it patches
    *existing* records via ``model_copy`` and adds the new outcome's
    ``delivery_attempts`` onto each record's prior count instead of
    building fresh records, and does its own per-record idempotency-key
    grouping besides -- differently shaped enough that forcing it through
    here would trade the duplication this removes for a pile of
    conditionals undoing most of what the helper buys.
    """
    response_events = (outcome.response or {}).get("events", []) if outcome.response else []
    paired_responses = _pair_event_responses(events, response_events)
    stored_records: list[StoredEventRecord] = []
    for index, event in enumerate(events):
        event_response = paired_responses[index]
        stored_records.append(
            StoredEventRecord(
                payload_source=source,
                event=event,
                parent_lot_codes=parent_lot_codes[index],
                destination_mode=delivery_mode,
                delivery_attempts=outcome.delivery_attempts,
                last_delivery_attempt_at=outcome.attempted_at,
                delivery_response=event_response,
                delivery_metadata=outcome.metadata,
                **_event_delivery_fields(outcome, event_response),
            )
        )
    return stored_records


def _validate_live_delivery(delivery: DeliveryConfig) -> None:
    if delivery.mode == DestinationMode.LIVE and (not delivery.api_key or not delivery.tenant_id):
        raise ValueError("Live delivery requires both api_key and tenant_id")


def _engine_shaping_fields_changed(previous: SimulationConfig, incoming: SimulationConfig) -> bool:
    """True if *incoming* differs from *previous* in a field LegitFlowEngine.reset() takes.

    ``seed``/``scenario``/``scale`` are the only three of SimulationConfig's
    fields that actually reshape the engine -- everything else (source,
    batch_size, interval_seconds, persist_path, delivery) is read fresh off
    ``self.config`` by ``step()``/``_run_loop()`` on every call, so changing
    those needs no reset to take effect. Shared by start()'s not-running
    reset gate and its running-conflict guard (#96) below, so the two field
    lists cannot drift apart if engine.reset()'s own parameters ever change.
    """
    return (
        incoming.seed != previous.seed
        or incoming.scenario != previous.scenario
        or incoming.scale != previous.scale
    )


def _stored_idempotency_key(record: StoredEventRecord) -> str | None:
    metadata = record.delivery_metadata or {}
    value = metadata.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None


def _is_idempotency_replay(response: dict[str, Any], payload: IngestPayload) -> bool:
    """True when *response* answers a differently-sized request than *payload* (#95).

    A genuinely fresh RegEngine response -- mock or live -- carries
    exactly one verdict per submitted event (MockRegEngineService.ingest
    appends one IngestResponseEvent per loop iteration over
    payload.events; live RegEngine's contract is the same). A repeated
    Idempotency-Key instead replays whatever response that key originally
    cached, verbatim and without revalidating anything -- correct when
    the replayed request is byte-for-byte the one that populated the
    cache, but wrong when it is only a subset (retry_failed_delivery
    resending fewer events than the original batch that owned the key).
    The subset case is exactly what a mismatched event count catches:
    nothing about a legitimate, matching replay ever trips it.
    """
    return len(response.get("events", [])) != len(payload.events)


def _aggregate_outcomes(outcomes: list[DeliveryOutcome]) -> DeliveryOutcome:
    """Roll up one DeliveryOutcome per chunk into the single summary
    import_csv/replay's response schemas need (#103).

    A single-chunk call -- everything at or under MAX_BATCH_EVENTS, still
    the overwhelming common case -- passes its one outcome through
    completely unchanged rather than rebuild it into a synthetic shape:
    CSVImportResponse.response / ReplayResponse.response stays exactly
    whatever mock_service/live_client actually returned (including
    fields like "total"/"ingestion_timestamp" a merged dict would
    otherwise drop), and every other field is that same single outcome's
    own.

    Multiple chunks combine by summing posted/failed/delivery_attempts,
    so an early chunk succeeding is never erased by a later one failing
    -- the concrete "partial failure mid-way through several chunks"
    case #103 asks for. delivery_status collapses to "failed" if *any*
    chunk failed outright (surfaced by callers as
    status="delivery_failed"/"failed"), to "generated" only when every
    chunk was (delivery mode NONE never calls RegEngine at all), and to
    "posted" otherwise -- the exact three values ReplayResponse's own
    ``{"posted": ..., "failed": ..., "generated": ...}[outcome.delivery_status]``
    lookup requires; see replay().

    This aggregate is for the one response object each caller returns,
    never for per-record fields -- building StoredEventRecords must use
    each chunk's own outcome instead (its own delivery_attempts, its own
    response for verdict pairing), which is exactly why import_csv calls
    _build_stored_records once per chunk rather than against this.
    """
    if not outcomes:
        return DeliveryOutcome()
    if len(outcomes) == 1:
        return outcomes[0]

    if any(outcome.delivery_status == "failed" for outcome in outcomes):
        delivery_status = "failed"
    elif all(outcome.delivery_status == "generated" for outcome in outcomes):
        delivery_status = "generated"
    else:
        delivery_status = "posted"

    error_messages = [outcome.error_message for outcome in outcomes if outcome.error_message]

    response_events: list[Any] = []
    any_response = False
    for outcome in outcomes:
        if outcome.response is not None:
            any_response = True
            response_events.extend(outcome.response.get("events", []))

    posted = sum(outcome.posted for outcome in outcomes)
    failed = sum(outcome.failed for outcome in outcomes)
    return DeliveryOutcome(
        response=(
            {
                "chunk_count": len(outcomes),
                "accepted": posted,
                "rejected": failed,
                "events": response_events,
            }
            if any_response
            else None
        ),
        delivery_status=delivery_status,
        posted=posted,
        failed=failed,
        delivery_attempts=sum(outcome.delivery_attempts for outcome in outcomes),
        error_message="; ".join(error_messages) or None,
    )

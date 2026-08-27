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


# Reported on a delivery response whose store write was dropped because the
# store was reset/reloaded/repointed while the delivery was in flight (#208).
# See SimulationController._store_epoch.
STALE_COMMIT_MESSAGE = (
    "Delivery completed, but the event store was reset, reloaded or repointed "
    "while it was in flight, so these records were not stored."
)


def _with_stale_commit_note(error_message: str | None) -> str:
    """Append the dropped-commit note to whatever the delivery itself reported."""
    if not error_message:
        return STALE_COMMIT_MESSAGE
    return f"{error_message}; {STALE_COMMIT_MESSAGE}"


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
        # Serializes retry_failed_delivery against itself, and nothing else
        # (#208). Every other delivery path builds its own events before
        # posting them, so two concurrent calls simply deliver two different
        # batches -- but retry *reads shared mutable state* (the store's
        # failed records) and then acts on it. self._lock used to provide
        # that mutual exclusion for free by being held across the whole
        # call; now that the POSTs happen with it released, two overlapping
        # retries would both select the same failed records and both post
        # them. This lock restores exactly that one guarantee without
        # putting step/reset/configure_integration back behind a delivery.
        #
        # Lock ordering: this one -> self._lock -> _lifecycle_lock, never
        # the reverse. Nothing but retry_failed_delivery takes this, and it
        # takes it first and outermost, so no cycle is possible.
        self._retry_lock = asyncio.Lock()
        self._revision = 0
        self._change_condition = asyncio.Condition()
        # Bumped whenever the store's *contents or identity* are replaced
        # wholesale: reset(), load_scenario_save()'s replace_all,
        # load_demo_fixture()'s reset, and any configure() that repoints
        # persist_path at a different file (#208).
        #
        # This is what makes releasing self._lock around a delivery safe.
        # A delivery path snapshots this value under the lock alongside the
        # records it is about to post, posts with the lock released, then
        # re-acquires and compares before committing. A mismatch means an
        # operator cleared, reloaded or repointed the store while the batch
        # was in flight, so the batch's records belong to a store that no
        # longer exists -- writing them would resurrect history the operator
        # just asked to be rid of, which is the one outcome #208 names as
        # unacceptable. The commit is dropped instead and reported on the
        # response's `error` field, so the caller learns the delivery
        # happened but nothing was stored rather than silently believing
        # either half.
        #
        # Deliberately NOT bumped by ordinary appends (add_many/update_many)
        # or by config changes: an append does not invalidate another
        # batch's records, and a config change mid-delivery is recorded
        # faithfully by each path using its own snapshot of the config it
        # actually delivered with.
        self._store_epoch = 0

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
            # The operator asked for an empty store. Any delivery already in
            # flight must not repopulate it when it comes back (#208) -- the
            # acceptance criterion this issue names explicitly.
            self._invalidate_store_epoch()
            self.mock_service.reset()
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        # --- Snapshot phase, under self._lock -------------------------------
        # Everything that touches shared mutable state -- reading the config,
        # pulling events off the engine -- happens here. `active_config` is
        # the one config this whole call uses from now on: SimulationConfig
        # is only ever replaced wholesale via model_copy, never mutated in
        # place, so this reference cannot be changed under us by a concurrent
        # start()/configure_integration(). A config change landing mid-flight
        # therefore applies to the *next* step, and this step's response and
        # stored records describe the config it actually delivered with (#208).
        async with self._lock:
            active_config = self.config
            _validate_live_delivery(active_config.delivery)
            size = batch_size or active_config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

            payload = IngestPayload(source=active_config.source, events=events)
            store_epoch = self._store_epoch

        # --- Delivery phase, with self._lock RELEASED (#208) ----------------
        # This is the await that used to pin the lock for a full 30s live
        # timeout, blocking start/reset/configure_integration/save_scenario
        # for this tenant behind one HTTP request.
        outcome = await self._deliver_payload(
            payload,
            active_config,
            idempotency_key=uuid.uuid4().hex,
        )

        # Pure CPU, no shared state -- build the records before re-acquiring
        # so the commit phase holds the lock for the store write alone.
        stored_records = _build_stored_records(
            active_config.source, events, lineages, active_config.delivery.mode, outcome
        )

        # --- Commit phase, back under self._lock ----------------------------
        stale = await self._commit_delivered_records(stored_records, store_epoch, "step")
        result = StepResponse(
            generated=len(events),
            posted=outcome.posted,
            accepted=(outcome.response or {}).get("accepted", 0),
            rejected=(outcome.response or {}).get("rejected", 0),
            failed=outcome.failed,
            lot_codes=[event.traceability_lot_code for event in events],
            delivery_status=outcome.delivery_status,
            delivery_mode=active_config.delivery.mode,
            delivery_attempts=outcome.delivery_attempts,
            response=outcome.response,
            error=_with_stale_commit_note(outcome.error_message) if stale else outcome.error_message,
        )
        await self._publish_update()
        return result

    async def replay(self, request: ReplayRequest | None = None) -> ReplayResponse:
        request = request or ReplayRequest()
        # --- Snapshot phase, under self._lock -------------------------------
        # Read the config and the persisted log, then let go. This is the
        # path #208 measures: at MAX_BATCH_EVENTS per chunk, a 25k-record
        # store is 50 sequential POSTs, so holding the lock across the loop
        # below pinned it for ~50 x the live timeout.
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
            # --- Delivery phase, with self._lock RELEASED (#208) ------------
            # replay() needs no commit phase and takes no store-epoch
            # snapshot, because it is the one delivery path that writes
            # nothing back: it re-posts records that are already persisted
            # and reports what RegEngine said. A reset() landing mid-replay
            # therefore has nothing of this call's to overwrite, and the
            # response stays truthful about what was actually POSTed -- the
            # events really were read from disk and really were sent, and
            # suppressing that afterwards would under-report deliveries the
            # remote side has already accepted.
            #
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
        # --- Snapshot phase, under self._lock -------------------------------
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
            store_epoch = self._store_epoch

        # CPU-bound parsing (#136): offloaded to a worker thread so a large
        # csv_text body does not tie up the event loop for the whole parse.
        # Now also outside self._lock (#208) -- it reads only the request
        # body, touches no controller state, and a multi-second parse is
        # exactly as good a reason to not be holding the lock as a
        # multi-second POST is.
        parsed = await asyncio.to_thread(
            parse_csv_import,
            request.import_type,
            request.csv_text,
            default_timestamp=datetime.now(UTC),
        )

        outcome = DeliveryOutcome()
        stored_records: list[StoredEventRecord] = []
        stale = False
        if parsed.events:
            # --- Delivery phase, with self._lock RELEASED (#208) ------------
            # #103: same MAX_BATCH_EVENTS cap as replay() -- a CSV
            # import has no row-count bound of its own, so a batch
            # parsed from a large file used to be posted as one
            # oversized request and 422 in full. Chunk here and build
            # stored records per chunk (each chunk keeps its own
            # outcome, so a record's delivery_attempts reflects the one
            # real POST it was actually part of, not the whole
            # import's chunk count). Every one of those chunk POSTs is now
            # awaited with the lock released, not just the first.
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
            # --- Commit phase, back under self._lock ------------------------
            stale = await self._commit_delivered_records(stored_records, store_epoch, "import_csv")
            if stale:
                # Nothing was persisted, so `stored` must report 0 rather
                # than the number of records this call built and threw away.
                stored_records = []
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
            error=_with_stale_commit_note(outcome.error_message) if stale else outcome.error_message,
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

        # --- Snapshot phase, under self._lock -------------------------------
        # The config/engine/store mutations this fixture load performs stay
        # here, committed before the delivery starts -- they are the load,
        # not a consequence of it, and a caller who sees this call return
        # must see them applied whatever the remote endpoint did.
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
            active_config = self.config
            await self._store_configure(active_config.persist_path)
            self.engine.reset(active_config.seed, scenario=fixture.scenario, scale=active_config.scale)
            if request.reset:
                self.store.reset()
                # This call's own clearing of the store must not make its own
                # commit look stale, so the epoch is snapshotted *after* it.
                self._invalidate_store_epoch()
                self.mock_service.reset()

            events = [fixture_event.event for fixture_event in fixture.events]
            payload = IngestPayload(source=source, events=events)
            store_epoch = self._store_epoch

        # --- Delivery phase, with self._lock RELEASED (#208) ----------------
        outcome = await self._deliver_payload(payload, active_config)
        fixture_parent_lot_codes = [
            list(fixture_event.parent_lot_codes) for fixture_event in fixture.events
        ]
        stored_records = _build_stored_records(
            source, events, fixture_parent_lot_codes, delivery.mode, outcome
        )

        # --- Commit phase, back under self._lock ----------------------------
        # A reset() (or a scenario load, or a start() repointing persist_path)
        # landing while the fixture was in flight wins: the fixture's records
        # are dropped rather than written into the store the operator just
        # cleared, and `stored` reports 0 so the caller is not told a fixture
        # is loaded that is not there.
        stale = await self._commit_delivered_records(stored_records, store_epoch, "load_demo_fixture")
        result = DemoFixtureLoadResponse(
            status="delivery_failed" if outcome.delivery_status == "failed" else "loaded",
            fixture_id=fixture.id,
            scenario=fixture.scenario,
            loaded=len(events),
            stored=0 if stale else len(stored_records),
            posted=outcome.posted,
            failed=outcome.failed,
            source=source,
            delivery_mode=delivery.mode,
            delivery_attempts=outcome.delivery_attempts,
            lot_codes=fixture.lot_codes,
            response=outcome.response,
            error=_with_stale_commit_note(outcome.error_message) if stale else outcome.error_message,
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
            # replace_all swaps the store's entire contents for the saved
            # snapshot's. A delivery in flight is holding records that belong
            # to the history this just replaced, so its commit is dropped
            # rather than allowed to append into the loaded scenario (#208).
            self._invalidate_store_epoch()
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
        # _retry_lock is held for the whole call, delivery included: unlike
        # every other delivery path, this one selects the batch it posts from
        # shared mutable state (the store's failed records), so two
        # overlapping calls would otherwise both select and both post the
        # same records now that the POSTs happen with self._lock released
        # (#208). It blocks nothing but another retry -- step, reset,
        # configure_integration and the rest never take it.
        async with self._retry_lock:
            result = await self._retry_failed_delivery_locked(request)
        await self._publish_update()
        return result

    async def _retry_failed_delivery_locked(
        self, request: DeliveryRetryRequest
    ) -> DeliveryRetryResponse:
        """The body of retry_failed_delivery; caller must hold ``_retry_lock``.

        Split out purely so the lock is acquired once, in one place, around
        all three of this path's phases -- snapshot under ``self._lock``,
        POST per group with it released, commit back under it -- rather
        than around a body with several early returns.
        """
        # --- Snapshot phase, under self._lock -------------------------------
        async with self._lock:
            base_config = self.config
            delivery = request.delivery or base_config.delivery
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

            # Group the candidates while still under the lock, so the
            # delivery loop below has everything it needs and touches no
            # controller state at all (#208).
            grouped_records: dict[tuple[str, str | None], list[StoredEventRecord]] = {}
            if candidates and delivery.mode != DestinationMode.NONE:
                _validate_live_delivery(delivery)
                for record in candidates:
                    source = request.source or record.payload_source
                    idempotency_key = (
                        _stored_idempotency_key(record)
                        if delivery.mode != DestinationMode.NONE
                        else None
                    )
                    grouped_records.setdefault((source, idempotency_key), []).append(record)
            store_epoch = self._store_epoch

        # Both of these answer without delivering anything, so they need no
        # lock of their own -- every value they read was snapshotted above.
        if not candidates:
            return DeliveryRetryResponse(
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
        if delivery.mode == DestinationMode.NONE:
            return DeliveryRetryResponse(
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

        # --- Delivery phase, with self._lock RELEASED (#208) ----------------
        # One POST per (source, idempotency_key) group; this loop is the
        # "minutes across a multi-group retry" #158 describes, and none of
        # it holds the data-plane lock any more.
        posted = 0
        failed = 0
        updated_records: list[StoredEventRecord] = []
        responses: list[dict[str, Any]] = []
        for (source, idempotency_key), records in grouped_records.items():
            retry_config = base_config.model_copy(
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
        # --- Commit phase, back under self._lock ----------------------------
        # A reset()/scenario-load/repoint landing mid-retry wins: the patched
        # records describe rows that no longer exist, and update_many would
        # rewrite the whole log from _all_records() on their behalf. Drop the
        # update instead; the POSTs themselves stay reported truthfully.
        stale = await self._commit_retried_records(updated_records, store_epoch)
        if posted and failed:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "posted"
        delivery_error = next((item["error"] for item in responses if item.get("error")), None)
        return DeliveryRetryResponse(
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
            error=_with_stale_commit_note(delivery_error) if stale else delivery_error,
        )

    # -- Committing a delivery whose await happened outside the lock (#208) --
    #
    # Every delivery path follows the same three phases: snapshot the config,
    # the events and self._store_epoch under self._lock; await the POST(s)
    # with the lock RELEASED; re-acquire to write the resulting records.
    # The helpers below are phase three, and exist so the "did the world
    # move underneath us" decision is made in exactly one place rather than
    # re-derived at four call sites.
    #
    # The epoch comparison and the store write happen under the same
    # uninterrupted hold of self._lock, which is what makes the check
    # meaningful: reset(), load_scenario_save() and load_demo_fixture() all
    # take self._lock to invalidate, so none of them can slip between the
    # comparison and the write.
    #
    # `await self._store_*` inside that hold is deliberate and is NOT what
    # #208 is about: it is a local-disk write offloaded to a worker thread,
    # bounded by disk speed, not a 30-second network timeout multiplied by
    # ceil(N/500) chunks.
    def _invalidate_store_epoch(self) -> None:
        """Mark every in-flight delivery's pending commit as stale (#208).

        Callers must already hold ``self._lock`` -- every one of them is
        inside a lock block that is also performing the wholesale store
        change this is announcing.
        """
        self._store_epoch += 1

    async def _commit_delivered_records(
        self,
        records: list[StoredEventRecord],
        store_epoch: int,
        path_name: str,
    ) -> bool:
        """Append *records* unless the store moved on; True if the commit was dropped."""
        async with self._lock:
            if self._store_epoch != store_epoch:
                logger.error(
                    "%s: discarding %d delivered record(s) -- the event store was reset, "
                    "reloaded or repointed while the delivery was in flight",
                    path_name,
                    len(records),
                )
                return True
            if records:
                await self._store_add_many(records)
            return False

    async def _commit_retried_records(
        self,
        records: list[StoredEventRecord],
        store_epoch: int,
    ) -> bool:
        """Patch *records* in place unless the store moved on; True if dropped.

        Separate from ``_commit_delivered_records`` because retry updates
        existing rows rather than appending new ones, and the stale case is
        sharper here: ``EventStore.update_many`` rewrites the entire log
        from ``_all_records()``, so letting it run against a store that was
        cleared mid-retry would rewrite a freshly emptied file on behalf of
        records that no longer exist.
        """
        async with self._lock:
            if self._store_epoch != store_epoch:
                logger.error(
                    "retry_failed_delivery: discarding %d retried record update(s) -- the "
                    "event store was reset, reloaded or repointed while the retry was in flight",
                    len(records),
                )
                return True
            await self._store_update_many(records)
            return False

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
        # A configure() that lands on a *different* file replaces the whole
        # in-memory ring from that file's contents, so any delivery in
        # flight is now holding records for a store that is no longer the
        # one on disk (#208). Bump the epoch so its commit is dropped.
        #
        # Only when the path actually changes: start() calls this on every
        # press of Start, almost always with the same persist_path, and
        # bumping unconditionally would let a plain start() throw away a
        # concurrent import's records for no reason. Compared before and
        # after rather than against the raw string so this tracks whatever
        # normalization EventStore.configure applies to it.
        previous_path = str(self.store.persist_path)
        await asyncio.to_thread(self.store.configure, persist_path)
        if str(self.store.persist_path) != previous_path:
            self._invalidate_store_epoch()

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


def merge_omitted_delivery_secrets(
    stored: DeliveryConfig, requested: DeliveryConfig
) -> DeliveryConfig:
    """Carry forward credentials a *request body* left out (#148).

    The console cannot round-trip these fields: ``status()`` runs every
    config through ``_sanitize_public_config``, which always strips
    ``api_key`` and also strips ``tenant_id`` in live mode, and
    ``applyConfigToForm()`` blanks the API-key input deliberately. So a
    ``delivery`` block that arrives with ``api_key=None`` means "I was not
    given one to send", never "clear the stored one" -- and treating it as
    the latter let a page reload silently strip a live credential on the
    operator's next Start / Clear shift / Retry / fixture load, downgrading
    or misdirecting delivery with no confirmation.

    This is the same contract ``/api/integration/configure`` already honors
    for its partial updates. It is applied at the HTTP boundary rather than
    inside the controller on purpose: a direct ``controller.reset(
    SimulationConfig())`` call means "give me a clean default" and must keep
    clearing everything.

    Credentials are never carried across to a *different* destination -- if
    the request names an endpoint that is not the stored one, the caller is
    pointing somewhere new and gets only what it sent.
    """
    if requested.api_key is not None and requested.tenant_id is not None:
        return requested
    if (
        requested.endpoint is not None
        and stored.endpoint is not None
        and str(requested.endpoint) != str(stored.endpoint)
    ):
        return requested
    updates: dict[str, Any] = {}
    if requested.api_key is None and stored.api_key:
        updates["api_key"] = stored.api_key
    if requested.tenant_id is None and stored.tenant_id:
        updates["tenant_id"] = stored.tenant_id
    if not updates:
        return requested
    return requested.model_copy(update=updates, deep=True)


def config_with_stored_delivery_secrets(
    active_controller: "SimulationController", config: SimulationConfig
) -> SimulationConfig:
    """``merge_omitted_delivery_secrets`` for a whole submitted config (#148)."""
    delivery = merge_omitted_delivery_secrets(active_controller.config.delivery, config.delivery)
    if delivery is config.delivery:
        return config
    return config.model_copy(update={"delivery": delivery}, deep=True)


def request_with_stored_delivery_secrets(
    active_controller: "SimulationController", request: Any | None
) -> Any | None:
    """``merge_omitted_delivery_secrets`` for a request body carrying an
    optional inline ``delivery`` block (retry, CSV import, fixture load,
    replay). A request that omits ``delivery`` entirely already falls back to
    the stored config inside the controller, so it is left untouched (#148).
    """
    if request is None or getattr(request, "delivery", None) is None:
        return request
    delivery = merge_omitted_delivery_secrets(active_controller.config.delivery, request.delivery)
    if delivery is request.delivery:
        return request
    return request.model_copy(update={"delivery": delivery}, deep=True)


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

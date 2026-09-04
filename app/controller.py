from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

from fastapi import HTTPException

from .audit import summarize_scenario_audit
from .contract import INFLOW_CONTRACT_VERSION
from .csv_importer import parse_csv_import
from .delivery import (
    DeliveryOutcome,
    aggregate_outcomes,
    build_stored_records,
    deliver_in_chunks,
    deliver_payload,
    event_delivery_fields,
    pair_event_responses,
    stored_idempotency_key,
)
from .demo_fixtures import get_demo_fixture
from .engine import LegitFlowEngine
# MAX_BATCH_EVENTS is imported here, not read inside app/delivery.py, so
# the chunk size the controller passes is this module's name -- the one
# tests shrink to force several small chunks (#103).
from .mock_service import MAX_BATCH_EVENTS, MockRegEngineService
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
)
from .schemas.integration import IntegrationConfigureRequest, IntegrationStatusResponse
from .scenario_saves import ScenarioSaveStore
from .scenarios import ScenarioId, get_scenario
from .store import EventStore


# Same name-keyed singleton logger main.py configures, so delivery
# failures land in the stream operators already watch (#182).
logger = logging.getLogger("inflow_lab")

# Response-status vocabularies, mirrored from the response models in
# app/schemas so the controller cannot hand the API a status its schema
# rejects -- mypy checks every assignment below against these.
ReplayStatus = Literal["empty", "posted", "rebuilt", "failed"]
CSVImportStatus = Literal["accepted", "partial", "rejected", "delivery_failed"]
RetryStatus = Literal["empty", "posted", "partial", "failed", "skipped"]
_REPLAY_STATUS_BY_DELIVERY: dict[str, ReplayStatus] = {
    "posted": "posted",
    "failed": "failed",
    "generated": "rebuilt",
}

DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_ENV = "REGENGINE_SHUTDOWN_TIMEOUT_SECONDS"

# What a caller is told when its delivery landed but the store moved underneath
# it. The events did reach the endpoint; what was abandoned is the local record
# of them, because writing it would have resurrected a store the operator just
# cleared. The machine-readable status cannot say this on its own -- the
# delivery genuinely succeeded -- so it goes in `error`.
STALE_COMMIT_NOTE = (
    "Delivery completed, but the store was reset or reconfigured while it was in "
    "flight, so these records were not committed locally."
)


def _shutdown_timeout_seconds() -> float:
    """Seconds to wait for a run loop to stop before cancelling it.

    Configurable because the right value is the platform's shutdown grace
    period, which this process cannot know. A malformed or non-positive value
    degrades to the default rather than raising at import time.
    """
    raw = os.getenv(SHUTDOWN_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "ignoring malformed %s=%r; using the %.1fs default",
            SHUTDOWN_TIMEOUT_ENV,
            raw,
            DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_SHUTDOWN_TIMEOUT_SECONDS


def _with_stale_note(error_message: str | None, committed: bool) -> str | None:
    """Fold the dropped-commit note into a response's `error` field.

    A caller has to be able to tell "your events were delivered and stored"
    from "your events were delivered and then thrown away".
    """
    if committed:
        return error_message
    if error_message:
        return f"{error_message} {STALE_COMMIT_NOTE}"
    return STALE_COMMIT_NOTE



class SimulationController:
    def __init__(
        self,
        engine: LegitFlowEngine,
        store: EventStore,
        scenario_saves: ScenarioSaveStore,
        mock_service: MockRegEngineService,
        live_client: LiveRegEngineClient,
        tenant_id: str = "default",
    ) -> None:
        self.engine = engine
        self.store = store
        self.scenario_saves = scenario_saves
        self.mock_service = mock_service
        self.live_client = live_client
        # Identifies this controller in log lines only. One process runs one
        # controller per tenant, and a failure that names no tenant is not
        # actionable on a shared deployment (#182).
        self.tenant_id = tenant_id
        self.config = SimulationConfig(persist_path=str(store.persist_path))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        # Guards the run-loop lifecycle -- `_task` and `_stop_event` -- and
        # nothing else. Deliberately NOT `self._lock`, the data-plane lock every
        # delivery path holds: `stop()` must be able to signal and clear a run
        # loop while a delivery is in flight, and a crashed loop must be
        # clearable while some other coroutine holds the data plane. Lock order,
        # where both are taken (only `start()`): `_lifecycle_lock` first, then
        # `_lock`. Nothing ever takes them the other way round.
        self._lifecycle_lock = asyncio.Lock()
        # Bumped, under `_lock`, whenever the store is cleared or repointed.
        # Delivery now runs outside `_lock` (#208), so between snapshotting what
        # to send and committing the outcome, a `reset()` or a fixture load can
        # land. The epoch is how the commit phase notices: if it changed, the
        # batch in flight describes a store that no longer exists and is dropped
        # rather than written over the new one. `_revision` is the wrong guard --
        # it counts every SSE-visible mutation, so a single background step
        # during a long replay would discard a perfectly good batch.
        self._store_epoch = 0
        self._revision = 0
        self._change_condition = asyncio.Condition()
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def revision(self) -> int:
        return self._revision

    async def start(self, config: SimulationConfig) -> None:
        # `_lifecycle_lock` is what makes this mutually exclusive with `stop()`
        # (#156): both mutate `_task`/`_stop_event`, and stop() no longer takes
        # the data-plane lock. `_lock` is still taken, inside it, for the config
        # and engine/store mutations below -- the one place both are held, and
        # always in this order.
        async with self._lifecycle_lock, self._lock:
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
            # configure() reloads the in-memory ring from the (possibly
            # different) file, so any batch snapshotted before this point
            # describes a store that is gone.
            self._store_epoch += 1
            if not self.running and _engine_shaping_fields_changed(previous_config, config):
                self.engine.reset(config.seed, scenario=config.scenario, scale=config.scale)
            if not self.running:
                self._last_error = None
                stop_event = asyncio.Event()
                self._stop_event = stop_event
                self._task = asyncio.create_task(self._run_loop(stop_event))
        await self._publish_update()

    async def stop(self) -> None:
        # #156: the _task/_stop_event mutation below must happen under
        # self._lifecycle_lock -- the same lock start() uses for it -- or a
        # concurrent start() can land in the gap this used to leave open. The old
        # body read self.running, set self._stop_event, and unconditionally
        # cleared self._task with no lock at all, so a start() landing
        # between "the awaited task finished" and "this coroutine resumed"
        # would install its own task/event and then have both silently
        # discarded by this method's own `self._task = None` -- orphaning
        # a live run loop that nothing could ever reach again.
        async with self._lifecycle_lock:
            task = self._task
            if task is None:
                return
            # Captured under the same lock as `task`, from the same read of
            # self._task's generation -- start() always assigns
            # self._stop_event and self._task together (see above), so this
            # is guaranteed to be the event *this* task is actually waiting
            # on, never a later start()'s fresh replacement.
            stop_event = self._stop_event
            stop_event.set()
        # A task that already finished -- because the run loop crashed (#211),
        # not because anyone called stop() -- must still be cleared and
        # published below. `stop()` used to early-return on `task.done()`, so
        # `self._task` stayed installed forever naming a dead loop, `running`
        # read False off it, and the SSE revision never moved: every open
        # console went on rendering a run that no longer existed. Awaiting a
        # finished task is skipped rather than harmful; the retrieval of its
        # result happens once, below, after it is out of `self._task`.
        if not task.done():
            # await the task OUTSIDE the lock. A fix that instead held
            # _lifecycle_lock across this await would serialise correctly but
            # would block a concurrent start() for the whole drain, which
            # tests/test_stop_start_race.py pins as a behaviour to keep.
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
        # Retrieve the finished task's outcome now that nothing else refers to
        # it. `_run_loop` swallows its own exceptions (it logs and publishes
        # instead of propagating), so this is normally a no-op -- but if a
        # future change ever lets one escape, asyncio would otherwise report it
        # as "Task exception was never retrieved" at garbage-collection time:
        # detached from the run, and outside the logger operators watch (#182).
        if not task.cancelled():
            task.exception()
        await self._publish_update()

    async def shutdown(self, timeout: float | None = None) -> None:
        """Stop the run loop, giving up rather than hanging on a wedged one.

        Container shutdown used to inherit whatever the run loop was waiting
        on. Delivery no longer holds `_lock`, so a stop signal lands promptly,
        but a single in-flight POST can still take the full live timeout (30s
        by default), and the platform's shutdown grace period is shorter than
        that. Cancel past the cap instead of waiting.
        """
        limit = timeout if timeout is not None else _shutdown_timeout_seconds()
        try:
            await asyncio.wait_for(self.stop(), timeout=limit)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "controller shutdown timed out after %.1fs; cancelling the run loop: tenant=%s",
                limit,
                self.tenant_id,
            )
            task = self._task
            if task is not None and not task.done():
                task.cancel()
                # Drain it so the cancellation actually completes before the
                # process exits, and so nothing is left unretrieved.
                with suppress(asyncio.CancelledError, Exception):
                    await task
            self._task = None

    async def reset(self, config: SimulationConfig | None = None) -> None:
        await self.stop()
        async with self._lock:
            if config is not None:
                self.config = config
            await self._store_configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
            self.store.reset()
            self.mock_service.reset()
            self._store_epoch += 1
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        # Phase 1 -- snapshot under the lock. The engine advances here because
        # its state is shared; everything the delivery needs is copied out so
        # phase 2 reads nothing off `self`. `self.config` is reassigned rather
        # than mutated by start/reset, so holding the reference is a real
        # snapshot.
        async with self._lock:
            _validate_live_delivery(self.config.delivery)
            step_config = self.config
            epoch = self._store_epoch
            size = batch_size or step_config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

        # Phase 2 -- deliver with the lock released.
        payload = IngestPayload(source=step_config.source, events=events)
        outcome = await self._deliver_payload(
            payload,
            step_config,
            idempotency_key=uuid.uuid4().hex,
        )

        # Phase 3 -- commit under the lock, unless the store moved.
        async with self._lock:
            committed = epoch == self._store_epoch
            stored_records = build_stored_records(
                step_config.source, events, lineages, step_config.delivery.mode, outcome
            )
            if committed:
                await self._store_add_many(stored_records)
            else:
                logger.warning(
                    "dropped a step commit: the store changed during delivery: tenant=%s events=%d",
                    self.tenant_id,
                    len(events),
                )

            result = StepResponse(
                generated=len(events),
                posted=outcome.posted,
                accepted=(outcome.response or {}).get("accepted", 0),
                rejected=(outcome.response or {}).get("rejected", 0),
                failed=outcome.failed,
                lot_codes=[event.traceability_lot_code for event in events],
                delivery_status=outcome.delivery_status,
                delivery_mode=step_config.delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                response=outcome.response,
                error=_with_stale_note(outcome.error_message, committed),
            )
        await self._publish_update()
        return result

    async def replay(self, request: ReplayRequest | None = None) -> ReplayResponse:
        request = request or ReplayRequest()
        # Phase 1 -- snapshot under the lock.
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

        # Phase 2 -- deliver with the lock released. Replay re-posts what is
        # already on disk and writes nothing back, so there is no phase 3 and no
        # epoch check: a concurrent reset cannot make this commit wrong, because
        # there is no commit to invalidate.
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
            # only grows). deliver_in_chunks splits `events` into
            # slices at that same cap and posts each as its own
            # request; aggregate_outcomes rolls the per-chunk results
            # back into one summary so an early chunk's posted events
            # are still reported even if a later chunk fails.
            chunks = await deliver_in_chunks(
                self._deliver_payload, source, events, replay_config, chunk_size=MAX_BATCH_EVENTS
            )
            outcome = aggregate_outcomes([chunk_outcome for _, chunk_outcome in chunks])
            replay_status = _REPLAY_STATUS_BY_DELIVERY[outcome.delivery_status]
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
        # Phase 1 -- snapshot under the lock.
        async with self._lock:
            epoch = self._store_epoch
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

        # Phase 2 -- deliver with the lock released.
        #
        # #103: same MAX_BATCH_EVENTS cap as replay() -- a CSV import has no
        # row-count bound of its own, so a batch parsed from a large file used
        # to be posted as one oversized request and 422 in full. Chunk here and
        # build stored records per chunk below (each chunk keeps its own
        # outcome, so a record's delivery_attempts reflects the one real POST it
        # was actually part of, not the whole import's chunk count).
        outcome = DeliveryOutcome()
        chunks: list[tuple[list[RegEngineEvent], DeliveryOutcome]] = []
        if parsed.events:
            chunks = await deliver_in_chunks(
                self._deliver_payload, source, parsed.events, import_config, chunk_size=MAX_BATCH_EVENTS
            )
            # Response-level counts only, aggregated across every chunk (#103) --
            # per-record fields below use each chunk's own outcome instead.
            outcome = aggregate_outcomes([chunk_outcome for _, chunk_outcome in chunks])

        # Phase 3 -- commit under the lock, unless the store moved.
        async with self._lock:
            committed = epoch == self._store_epoch
            stored_records: list[StoredEventRecord] = []
            if chunks:
                lineage_offset = 0
                for chunk_events, chunk_outcome in chunks:
                    chunk_len = len(chunk_events)
                    stored_records.extend(
                        build_stored_records(
                            source,
                            chunk_events,
                            parsed.parent_lot_codes[lineage_offset : lineage_offset + chunk_len],
                            delivery.mode,
                            chunk_outcome,
                        )
                    )
                    lineage_offset += chunk_len
                if committed:
                    await self._store_add_many(stored_records)
                else:
                    logger.warning(
                        "dropped a CSV import commit: the store changed during delivery: "
                        "tenant=%s events=%d",
                        self.tenant_id,
                        len(parsed.events),
                    )

            rejected = parsed.total - len(parsed.events)
            status: CSVImportStatus
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
                # `stored` is what reached the store, so a dropped commit
                # reports 0 rather than the size of the batch it threw away.
                stored=len(stored_records) if committed else 0,
                posted=outcome.posted,
                failed=outcome.failed,
                source=source,
                delivery_mode=delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                lot_codes=[event.traceability_lot_code for event in parsed.events],
                errors=parsed.errors,
                warnings=parsed.warnings,
                response=outcome.response,
                error=_with_stale_note(outcome.error_message, committed),
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
            # Bumped, then read, AFTER this path's own reset -- so a fixture
            # load does not treat its own clearing of the store as somebody
            # else's and drop the very records it just generated.
            self._store_epoch += 1
            epoch = self._store_epoch
            fixture_config = self.config
            events = [fixture_event.event for fixture_event in fixture.events]

        # Phase 2 -- deliver with the lock released.
        payload = IngestPayload(source=source, events=events)
        outcome = await self._deliver_payload(payload, fixture_config)

        # Phase 3 -- commit under the lock, unless the store moved.
        async with self._lock:
            committed = epoch == self._store_epoch
            fixture_parent_lot_codes = [
                list(fixture_event.parent_lot_codes) for fixture_event in fixture.events
            ]
            stored_records = build_stored_records(
                source, events, fixture_parent_lot_codes, delivery.mode, outcome
            )
            if committed:
                await self._store_add_many(stored_records)
            else:
                logger.warning(
                    "dropped a fixture-load commit: the store changed during delivery: "
                    "tenant=%s fixture=%s events=%d",
                    self.tenant_id,
                    fixture.id.value,
                    len(events),
                )

            fixture_status: Literal["loaded", "delivery_failed"]
            if outcome.delivery_status == "failed" or not committed:
                # A load that stored nothing must not answer "loaded".
                fixture_status = "delivery_failed"
            else:
                fixture_status = "loaded"

            result = DemoFixtureLoadResponse(
                status=fixture_status,
                fixture_id=fixture.id,
                scenario=fixture.scenario,
                loaded=len(events),
                stored=len(stored_records) if committed else 0,
                posted=outcome.posted,
                failed=outcome.failed,
                source=source,
                delivery_mode=delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                lot_codes=fixture.lot_codes,
                response=outcome.response,
                error=_with_stale_note(outcome.error_message, committed),
            )
        await self._publish_update()
        return result

    async def list_scenario_saves(self) -> ScenarioSaveListResponse:
        # `list()` reads and JSON-parses one file per scenario. On the event
        # loop that is a stall on a plain listing, so it goes to a thread (#216).
        # No lock: a listing takes no consistent view across saves.
        snapshots = await asyncio.to_thread(self.scenario_saves.list)
        return ScenarioSaveListResponse(
            saves=[self._scenario_save_summary(snapshot) for snapshot in snapshots]
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
            # Offloaded (#216) but deliberately still under `self._lock`.
            # Hoisting the write out would let a `reset()` land between
            # snapshotting `records` above and writing them here, so the save
            # would persist a store state that never existed. Holding a lock
            # across an await is what the delivery paths must not do (#208) --
            # but this await is local disk, bounded by the filesystem, not by a
            # remote endpoint's timeout. Two tests pin both halves: that the
            # lock is held at the moment the write runs, and that a real
            # concurrent `reset()` cannot tear the saved snapshot.
            snapshot = await asyncio.to_thread(
                self.scenario_saves.save_snapshot,
                scenario_id,
                config,
                records,
            )
            result = ScenarioSaveResponse(
                status="saved",
                save=self._scenario_save_summary(snapshot),
                config=snapshot.config,
            )
        return result

    async def load_scenario_save(self, scenario_id: ScenarioId) -> ScenarioLoadResponse:
        await self.stop()
        # Read outside the lock, as it already was: nothing here needs a
        # consistent view of the store, and the reconfigure below takes the
        # lock for itself. Offloaded because `get()` reads and parses a whole
        # snapshot file (#216).
        snapshot = await asyncio.to_thread(self.scenario_saves.get, scenario_id)
        if snapshot is None:
            raise KeyError(scenario_id.value)

        async with self._lock:
            self.config = snapshot.config
            await self._store_configure(self.config.persist_path)
            loaded_records = await self._store_replace_all(snapshot.records)
            # replace_all rewrites the whole log: any batch snapshotted before
            # this point describes a store that no longer exists.
            self._store_epoch += 1
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
        # Set in phase 1 for the three cases that deliver nothing; left None so
        # phase 2 knows there is a batch to send.
        result: DeliveryRetryResponse | None = None
        # Phase 1 -- snapshot under the lock.
        async with self._lock:
            epoch = self._store_epoch
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
                retry_config_base = self.config
                grouped_records: dict[tuple[str, str | None], list[StoredEventRecord]] = {}
                for record in candidates:
                    source = request.source or record.payload_source
                    idempotency_key = (
                        stored_idempotency_key(record)
                        if delivery.mode != DestinationMode.NONE
                        else None
                    )
                    grouped_records.setdefault((source, idempotency_key), []).append(record)

        # Phase 2 -- deliver every group with the lock released. `result` is
        # already set for the three cases that never deliver.
        if result is None:
            posted = 0
            failed = 0
            updated_records: list[StoredEventRecord] = []
            responses: list[dict[str, Any]] = []
            for (source, idempotency_key), records in grouped_records.items():
                retry_config = retry_config_base.model_copy(
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

                paired_responses = pair_event_responses(
                    [record.event for record in records], response_events
                )
                for index, record in enumerate(records):
                    event_response = paired_responses[index]
                    next_attempts = record.delivery_attempts + outcome.delivery_attempts
                    fields = event_delivery_fields(outcome, event_response)
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

            # Phase 3 -- commit under the lock, unless the store moved.
            # This one matters most: update_many rewrites the whole log, so
            # applying it after a reset() would put every record the
            # operator just deleted back on disk. Safe to drop and repeat:
            # an attempt that produced no verdict reuses its original
            # idempotency key (#95).
            async with self._lock:
                committed = epoch == self._store_epoch
                if committed:
                    await self._store_update_many(updated_records)
                else:
                    logger.warning(
                        "dropped a delivery-retry commit: the store changed during "
                        "delivery: tenant=%s records=%d",
                        self.tenant_id,
                        len(updated_records),
                    )
            status: RetryStatus
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
                error=_with_stale_note(
                    next((item["error"] for item in responses if item.get("error")), None),
                    committed,
                ),
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
        """The one seam every delivery path posts through.

        The mechanics live in app/delivery.py (#212). This stays a bound
        method on purpose: all five paths and `deliver_in_chunks` call
        `self._deliver_payload`, so patching this name intercepts every
        POST exactly as it did before the split -- and `mock_service` /
        `live_client` are read off `self` here, per call, so swapping either
        attribute on a controller still takes effect. Nothing else in phase
        2 reads `self`: `config` is the snapshot phase 1 took under the lock.
        """
        return await deliver_payload(
            self.mock_service,
            self.live_client,
            payload,
            config,
            idempotency_key=idempotency_key,
            tenant_id=self.tenant_id,
        )

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
        try:
            while not stop_event.is_set():
                await self.step(self.config.batch_size)
                if self.config.interval_seconds <= 0:
                    await asyncio.sleep(0)
                else:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=self.config.interval_seconds)
                    except asyncio.TimeoutError:
                        continue
        except asyncio.CancelledError:
            # shutdown() cancels a wedged loop; that is not a run failure, and
            # publishing here would await inside a cancellation.
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "simulation run loop stopped on an unhandled error: tenant=%s",
                self.tenant_id,
            )
            # The run is over either way -- tell subscribers, so the console
            # stops showing it as running. Deliberately not re-raised: the
            # failure is already logged with its traceback and published, and
            # re-raising would leave an exception on a task that, on the crash
            # path, nothing is guaranteed to await.
            await self._publish_update()
        else:
            # A clean stop is a state change too: the loop bumps the revision as
            # it unwinds, before stop()'s own post-await bump. Not a `finally`,
            # because that branch also runs under cancellation, where awaiting
            # anything is the wrong thing to do.
            await self._publish_update()

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
        result: dict[str, Any] = {
            "running": self.running,
            "config": self._sanitize_public_config(self.config).model_dump(mode="json"),
            "stats": {
                **self.store.stats(),
                "audit": summarize_scenario_audit(records, scenario),
                "engine": self.engine.snapshot(),
            },
        }
        if self._last_error is not None:
            result["last_error"] = self._last_error
        return result

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


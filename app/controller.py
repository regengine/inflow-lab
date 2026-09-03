from __future__ import annotations

import asyncio
<<<<<<< HEAD
import logging
import os
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse
=======
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
>>>>>>> origin/main

from .audit import summarize_scenario_audit
from .csv_importer import parse_csv_import
from .delivery import (
    DeliveryOutcome,
    aggregate_outcomes,
    build_stored_records,
    chunk_events,
    deliver_payload,
    event_delivery_fields,
    pair_event_responses,
    rebuild_retried_records,
    retry_idempotency_key,
)
from .integration_config import (
    apply_integration_updates,
    build_integration_status,
    merge_delivery_credentials,
    preserve_delivery_credentials,
    sanitize_public_config,
    sanitize_saved_config,
    validate_live_delivery,
)
from .demo_fixtures import get_demo_fixture
from .engine import LegitFlowEngine
<<<<<<< HEAD
from .mock_service import MockRegEngineHTTPError, MockRegEngineService
from .regengine_client import (
    DEFAULT_LIVE_INGEST_ENDPOINT,
    WEBHOOK_HMAC_SECRET_ENV,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
)
from .scenario_saves import ScenarioSaveStore
from .scenarios import ScenarioId, get_scenario
from .schemas.domain import DemoFixtureId, DestinationMode, RegEngineEvent, StoredEventRecord
=======
from .mock_service import MockRegEngineService
from .schemas.domain import DemoFixtureId, DestinationMode, StoredEventRecord
>>>>>>> origin/main
from .schemas.ingestion import (
    CSVImportRequest,
    CSVImportResponse,
    DeliveryRetryRequest,
    DeliveryRetryResponse,
    IngestPayload,
    ReplayRequest,
    ReplayResponse,
)
from .schemas.integration import IntegrationConfigureRequest, IntegrationStatusResponse
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
<<<<<<< HEAD
from .schemas.simulation import (
    DeliveryConfig,
    SimulationConfig,
    StepResponse,
)
from .store import EventStore, mask_secret_in_payload, mask_secret_in_string
=======
from .schemas.simulation import SimulationConfig, StepResponse
from .regengine_client import LiveRegEngineClient
from .runtime_guard import MultiProcessRuntimeError, enforce_single_process_runtime
from .schemas.integration import IntegrationConfigureRequest, IntegrationStatusResponse
from .scenario_saves import ScenarioSaveStore
from .scenarios import ScenarioId, get_scenario
from .store import EventStore
>>>>>>> origin/main

logger = logging.getLogger(__name__)

DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

_REPLAY_STATUS: dict[str, Literal["posted", "rebuilt", "failed"]] = {
    "posted": "posted",
    "failed": "failed",
    "generated": "rebuilt",
}

# What a caller is told when its delivery landed but the store moved underneath
# it. The events did reach the endpoint; what was abandoned is the local record
# of them, because writing it would have resurrected a store the operator just
# cleared.
STALE_COMMIT_NOTE = (
    "Delivery completed, but the store was reset or reconfigured while it was in "
    "flight, so these records were not committed locally."
)


def _shutdown_timeout_seconds() -> float:
    try:
        value = float(
            os.getenv("REGENGINE_SHUTDOWN_TIMEOUT_SECONDS", str(DEFAULT_SHUTDOWN_TIMEOUT_SECONDS))
        )
    except ValueError:
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_SHUTDOWN_TIMEOUT_SECONDS


<<<<<<< HEAD
@dataclass(slots=True)
class DeliveryOutcome:
    response: dict[str, Any] | None = None
    delivery_status: Literal["generated", "posted", "failed"] = "generated"
    posted: int = 0
    failed: int = 0
    delivery_attempts: int = 0
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None
=======
logger = logging.getLogger(__name__)


# The single-process guard and the delivery-config vocabulary now live in
# ``app.runtime_guard`` and ``app.integration_config``. `MultiProcessRuntimeError`
# and `enforce_single_process_runtime` stay importable from here because that is
# where the controller enforces them and where tests reach for them.

# Delivery mechanics now live in ``app.delivery``. These names stay importable
# from ``app.controller`` because tests and older callers reach for them here.
_pair_event_responses = pair_event_responses
_event_delivery_fields = event_delivery_fields


# Every delivery path is snapshot -> deliver -> commit: it reads what it needs
# under `_lock`, releases the lock for the whole network round trip (every
# chunk of a chunked send), then re-acquires to write the outcome back. That is
# what keeps a 25k-record replay from pinning the lock for the length of 50
# sequential HTTP timeouts (#208), but it opens a window in which the world can
# move: a `reset()`, a `load_scenario_save()`, a demo-fixture load or a `start()`
# onto a different persist path can all land between the snapshot and the
# commit.
#
# `_store_epoch` is the guard. It is bumped -- under `_lock` -- by exactly those
# operations that replace the event log a batch was snapshotted from. A path
# carries the epoch it read at snapshot time and re-checks it while still
# holding the lock it will commit under, so check-and-write is atomic: a reset
# either lands before the check (and the batch is discarded) or after the
# commit (and clears it), never in between.
#
# `_revision` is deliberately *not* used for this. It is a change-notification
# counter for the console's SSE stream: `_publish_update()` bumps it after every
# mutation, including each successful `step()` and every
# `configure_integration()`. Guarding on it would mean a single background step
# firing during a long replay invalidated a batch whose store was never touched
# -- correct results thrown away for a notification. `_store_epoch` moves only
# when the data a batch was built from is genuinely gone.
STALE_COMMIT_ERROR = (
    "The event log was reset or reconfigured while this batch was in flight, so "
    "its records were not written back. They were delivered, but storing them "
    "would have refilled a log the operator had just cleared."
)

STALE_DELIVERY_ABORTED_ERROR = (
    "The event log was reset or reconfigured while this batch was in flight, so "
    "the remaining events were not sent."
)

# How long `shutdown()` waits for an in-flight delivery before cancelling the
# run loop outright. Container shutdown must not inherit a live endpoint's
# request timeout, let alone one per tenant.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

__all__ = [
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "STALE_COMMIT_ERROR",
    "STALE_DELIVERY_ABORTED_ERROR",
    "DeliveryOutcome",
    "MultiProcessRuntimeError",
    "SimulationController",
    "enforce_single_process_runtime",
]
>>>>>>> origin/main


class SimulationController:
    def __init__(
        self,
        engine: LegitFlowEngine,
        store: EventStore,
        scenario_saves: ScenarioSaveStore,
        mock_service: MockRegEngineService,
        live_client: LiveRegEngineClient,
        tenant_id: str | None = None,
    ) -> None:
<<<<<<< HEAD
        # Only used to identify this controller in log lines. The default
        # controller serves the shared store, so it is labelled as such rather
        # than left blank.
        self.tenant_id = tenant_id or "default"
=======
        # Checked here rather than in an entrypoint so it holds for every way
        # the app is started -- uvicorn, gunicorn, a script, a test harness.
        enforce_single_process_runtime()
>>>>>>> origin/main
        self.engine = engine
        self.store = store
        self.scenario_saves = scenario_saves
        self.mock_service = mock_service
        self.live_client = live_client
        # Let the mock rebuild its hash chain from whatever this controller has
        # already persisted, so a restart continues the chain instead of
        # forking it. Resumption is lazy, so this costs nothing at startup.
        self.mock_service.attach_event_source(store)
        self.config = SimulationConfig(persist_path=str(store.persist_path))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
<<<<<<< HEAD
        # Bumped whenever the store is cleared or repointed. Delivery now runs
        # outside `_lock` (#208), so between snapshotting what to send and
        # committing the outcome, a `reset()` or a fixture load can land. The
        # epoch is how the commit phase notices: if it changed, the batch in
        # flight describes a store that no longer exists and is dropped rather
        # than written over the new one.
        self._store_epoch = 0
=======
        # Guards run-loop lifecycle (`_task`/`_stop_event`) and keeps
        # stop-then-reconfigure sequences atomic against a concurrent start().
        # The run loop itself never takes this lock, so it is always safe to
        # await the loop task while holding it.
        self._lifecycle_lock = asyncio.Lock()
>>>>>>> origin/main
        self._revision = 0
        self._change_condition = asyncio.Condition()
        # Why the last run loop stopped, when it stopped by raising. Cleared
        # when a fresh loop is launched; surfaced by `status()`.
        self._last_run_error: str | None = None
        # Bumped whenever the event log a delivery was snapshotted from is
        # replaced. See the module comment above STALE_COMMIT_ERROR.
        self._store_epoch = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def store_epoch(self) -> int:
        """Identity of the current event log, for snapshot/commit guarding."""
        return self._store_epoch

    def _invalidate_in_flight_deliveries(self) -> None:
        """Declare every in-flight batch stale. Caller must hold `_lock`.

        Called by the operations that clear or swap the event log. Bumping
        under the lock is what lets a commit that also holds the lock trust
        its own comparison.
        """
        self._store_epoch += 1

    def _store_moved(self, epoch: int) -> bool:
        """Whether the log a batch was snapshotted from is still in place.

        Safe to call without `_lock` as a mid-flight early-out -- it is one
        int comparison and the only writer bumps under the lock, so the worst
        case is noticing a beat late. The *commit* check is always made while
        holding the lock the write happens under, which is where atomicity
        actually comes from.
        """
        return self._store_epoch != epoch

    async def start(self, config: SimulationConfig) -> None:
<<<<<<< HEAD
        async with self._lock:
            _validate_live_delivery(config.delivery)
            previous_config = self.config
            self.config = config
            self.store.configure(config.persist_path)
            self._store_epoch += 1
            if not self.running and (
=======
        """Apply `config` and make sure a run loop is running under it.

        Starting an already-running simulation with a different scenario,
        seed, scale or persist path used to assign the new config while
        leaving the old engine running: `status()` reported the new scenario
        (and audited accumulated events against it) while
        `stats.engine.scenario` still reported the old one, so one payload
        contradicted itself (#96). The engine-shaping fields are now applied
        the same way `load_demo_fixture` applies them -- stop the loop, swap
        the config, reset the engine, restart -- so the two halves of a
        status response can never disagree.

        `batch_size` and `interval_seconds` stay live-adjustable: they shape
        the loop's pacing, not the events it generates, so changing them does
        not restart anything.
        """
        async with self._lifecycle_lock:
            async with self._lock:
                config = preserve_delivery_credentials(self.config.delivery, config)
                validate_live_delivery(config.delivery)
                previous_config = self.config
            engine_changed = (
>>>>>>> origin/main
                config.seed != previous_config.seed
                or config.scenario != previous_config.scenario
                or config.scale != previous_config.scale
            )
            storage_changed = config.persist_path != previous_config.persist_path
            was_running = self.running
            if was_running and (engine_changed or storage_changed):
                # Stopping happens outside `_lock` on purpose: the run loop
                # takes `_lock` in `step()`, so awaiting it while holding the
                # lock would deadlock.
                await self._stop_run_loop()
            async with self._lock:
                self.config = config
                await self._configure_store(config.persist_path)
                if engine_changed:
                    self.engine.reset(config.seed, scenario=config.scenario, scale=config.scale)
                if engine_changed or storage_changed:
                    # A new persist path is a different log, and a reset engine
                    # restarts lot lineage: records snapshotted from the old
                    # one no longer belong here.
                    self._invalidate_in_flight_deliveries()
            if not self.running:
                self._launch_run_loop()
        await self._publish_update()

<<<<<<< HEAD
    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        if not task.done():
            await task
        elif not task.cancelled():
            # A run loop that ended on its own leaves a finished task
            # installed. Gating on `self.running` here -- which a done task
            # makes False -- meant `stop()` returned without clearing it or
            # bumping the revision. (`start()` does replace such a task, so
            # this never blocked a restart; what it lost was the state change
            # subscribers needed to see.) Retrieve any exception so asyncio
            # does not log it as never retrieved.
            task.exception()
        # Only clear the task this call was actually waiting on. `await task`
        # yields, and a `start()` landing in that window sees `running` False
        # (the old task is already done) and installs a new one -- which an
        # unconditional clear then orphaned: a live run loop with `_task` None,
        # unreachable by stop(), reset() or shutdown(), still writing records.
        if self._task is task:
            self._task = None
        await self._publish_update()

    async def shutdown(self) -> None:
        """Stop the run loop, giving up rather than hanging on a wedged one.

        Container shutdown used to inherit whatever the run loop was waiting
        on. Delivery no longer holds `_lock`, so a stop signal lands promptly,
        but a single in-flight POST can still take the full live timeout (30s
        by default), and the platform's shutdown grace period is shorter than
        that. Cancel instead of waiting past the cap.
        """
        try:
            await asyncio.wait_for(self.stop(), timeout=_shutdown_timeout_seconds())
        except TimeoutError:
            logger.warning(
                "Controller shutdown timed out after %ss; cancelling the run loop: tenant=%s",
                _shutdown_timeout_seconds(),
                self.tenant_id,
            )
            task = self._task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
=======
    def _launch_run_loop(self) -> None:
        """Start a fresh run loop. Caller must hold `_lifecycle_lock`."""
        self._last_run_error = None
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        # The loop owns its own stop event, so a superseded loop can never be
        # kept alive by a later start() swapping `self._stop_event`.
        self._task = asyncio.create_task(self._run_loop(stop_event))

    async def _stop_run_loop(self) -> bool:
        """Stop and await the current run loop. Caller must hold `_lifecycle_lock`.

        Returns True when a live loop was actually stopped.
        """
        task = self._task
        if task is None:
            return False
        self._task = None
        if task.done():
            # The loop ended on its own -- normally, or by raising. Retrieve
            # the exception so asyncio does not report it as unhandled at some
            # arbitrary later GC, and report a state change either way: a
            # `stop()` that follows a crash must still publish, or a console
            # blocked in `wait_for_revision` never learns the run is over.
            with contextlib.suppress(asyncio.CancelledError):
                task.exception()
            return True
        self._stop_event.set()
        await task
        return True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            stopped = await self._stop_run_loop()
        if stopped:
            await self._publish_update()

    async def shutdown(self, timeout: float | None = None) -> None:
        """Stop the run loop, optionally giving up on an in-flight delivery.

        `stop()` awaits the run-loop task, and that task can be parked on a
        delivery for the full live-endpoint timeout. That is fine for an
        operator pressing Stop -- the signal lands immediately, because
        `_lifecycle_lock` is not the data-plane lock (#156) -- but it is not
        fine for container shutdown, which would otherwise inherit one
        request timeout per tenant. With a `timeout` the wait is bounded and
        the loop is cancelled outright once it expires.
        """
        if timeout is None:
            await self.stop()
            return
        # Captured before `stop()` runs: `_stop_run_loop` clears `_task` up
        # front, so after a cancelled stop there would be nothing left to
        # cancel.
        task = self._task
        try:
            await asyncio.wait_for(self.stop(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "Run loop did not stop within %.1fs; cancelling it mid-delivery", timeout
            )
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
>>>>>>> origin/main
                    await task
            self._task = None

    async def reset(self, config: SimulationConfig | None = None) -> None:
<<<<<<< HEAD
        await self.stop()
        async with self._lock:
            if config is not None:
                self.config = config
            self.store.configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
            self.store.reset()
            self.mock_service.reset()
            self._store_epoch += 1
=======
        async with self._lifecycle_lock:
            await self._stop_run_loop()
            async with self._lock:
                if config is not None:
                    self.config = preserve_delivery_credentials(self.config.delivery, config)
                await self._configure_store(self.config.persist_path)
                self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
                self.store.reset()
                self.mock_service.reset()
                # Anything already delivered but not yet committed belongs to
                # the log this just cleared. Marking it stale here, under the
                # same lock that cleared the store, is what stops an in-flight
                # batch from refilling it (#208).
                self._invalidate_in_flight_deliveries()
>>>>>>> origin/main
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        # Phase 1 -- snapshot under the lock. The engine advances here because
        # its state is shared; everything the delivery needs is copied out so
        # phase 2 does not read `self` at all.
        async with self._lock:
<<<<<<< HEAD
            _validate_live_delivery(self.config.delivery)
            step_config = self.config
=======
            validate_live_delivery(self.config.delivery)
            step_config = self.config.model_copy(deep=True)
>>>>>>> origin/main
            epoch = self._store_epoch
            size = batch_size or step_config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

<<<<<<< HEAD
        # Phase 2 -- deliver with the lock released.
=======
        # Delivery can block on the network for the full live timeout, so it
        # happens outside the controller lock — start/stop/status stay responsive.
>>>>>>> origin/main
        payload = IngestPayload(source=step_config.source, events=events)
        outcome = await self._deliver_payload(
            payload,
            step_config,
            idempotency_key=uuid.uuid4().hex,
        )

<<<<<<< HEAD
        # Phase 3 -- commit under the lock, unless the store moved.
        async with self._lock:
            committed = epoch == self._store_epoch
            stored_records = _build_stored_records(
                events=events,
                lineages=lineages,
                source=step_config.source,
                delivery_mode=step_config.delivery.mode,
                outcome=outcome,
            )
            if committed:
                self.store.add_many(stored_records)
            else:
                logger.warning(
                    "Dropped a step commit: the store changed during delivery: tenant=%s events=%d",
                    self.tenant_id,
                    len(events),
                )
=======
        stored_records = build_stored_records(
            source=step_config.source,
            events=events,
            parent_lot_codes=lineages,
            delivery_mode=step_config.delivery.mode,
            outcome=outcome,
        )

        async with self._lock:
            # A manual /step does not take `_lifecycle_lock`, so unlike the run
            # loop's own steps it can be mid-delivery when a reset lands. The
            # batch is then dropped rather than appended: the operator asked
            # for an empty log and gets one.
            stale = self._store_moved(epoch)
            if not stale:
                await self._persist_new_records(stored_records)
>>>>>>> origin/main

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
<<<<<<< HEAD
                error=_with_stale_note(outcome.error_message, committed),
=======
                error=STALE_COMMIT_ERROR if stale else outcome.error_message,
>>>>>>> origin/main
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
            validate_live_delivery(delivery)
            replay_config = self.config.model_copy(
                update={
                    "source": source,
                    "delivery": delivery,
                },
                deep=True,
            )
            # Reading the whole log is an open plus a JSON parse per line; a
            # 25k-record store is enough to stall the loop noticeably, and the
            # stall would land on health checks, the SSE stream and every
            # other tenant. Off the loop, still under `_lock` so the records
            # and the config they are replayed with are one snapshot.
            records = await asyncio.to_thread(self.store.read_persisted_records, persist_path)
            events = [record.event for record in records]
            epoch = self._store_epoch

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
<<<<<<< HEAD
            # Phase 2 -- deliver with the lock released. Replay re-posts what is
            # already on disk and writes nothing back, so there is no phase 3
            # and no epoch check: a concurrent `reset()` cannot make this commit
            # wrong, because there is no commit.
            payload = IngestPayload(source=source, events=events)
            outcome = await self._deliver_payload(payload, replay_config)
            replay_status = _REPLAY_STATUS[outcome.delivery_status]
            result = ReplayResponse(
                status=replay_status,
                read=len(records),
                replayed=len(events),
=======
            # Delivery happens outside the controller lock; a slow live
            # endpoint must not block start/stop/status. Replaying a store
            # that has grown past the 500-event batch cap is the steady state
            # on a volume-backed deployment, so the read is delivered in
            # slices rather than as one oversized payload (#103).
            #
            # Replay writes nothing: it re-sends what is already stored, so
            # there is no commit for a reset to overwrite. What a reset does
            # invalidate is the *rest of the send* -- the remaining chunks
            # describe a log the operator has just cleared, and a 25k-record
            # replay has ~50 of them. So the epoch is checked between chunks
            # and the run is abandoned where it stands; `replayed` reports what
            # actually went out, which is why it can be lower than `read`.
            chunk_outcomes = []
            replayed = 0
            abandoned = False
            for chunk in chunk_events(events):
                if self._store_moved(epoch):
                    abandoned = True
                    break
                chunk_payload = IngestPayload(source=source, events=chunk)
                chunk_outcomes.append(await self._deliver_payload(chunk_payload, replay_config))
                replayed += len(chunk)
            outcome = aggregate_outcomes(chunk_outcomes)
            replay_status = {
                "posted": "posted",
                "failed": "failed",
                "generated": "rebuilt",
            }[outcome.delivery_status]
            if abandoned:
                replay_status = "failed"
            result = ReplayResponse(
                status=replay_status,
                read=len(records),
                replayed=replayed,
>>>>>>> origin/main
                posted=outcome.posted,
                failed=outcome.failed,
                source=source,
                persist_path=persist_path,
                delivery_mode=delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                response=outcome.response,
<<<<<<< HEAD
                error=outcome.error_message,
=======
                error=STALE_DELIVERY_ABORTED_ERROR if abandoned else outcome.error_message,
>>>>>>> origin/main
            )
        await self._publish_update()
        return result

    async def import_csv(self, request: CSVImportRequest) -> CSVImportResponse:
        # Phase 1 -- snapshot under the lock.
        async with self._lock:
            source = request.source or self.config.source
            delivery = request.delivery or self.config.delivery
            validate_live_delivery(delivery)
            import_config = self.config.model_copy(
                update={
                    "source": source,
                    "delivery": delivery,
                },
                deep=True,
            )
            epoch = self._store_epoch
<<<<<<< HEAD
            parsed = parse_csv_import(
                request.import_type,
                request.csv_text,
                default_timestamp=datetime.now(UTC),
            )

        # Phase 2 -- deliver with the lock released.
        outcome = DeliveryOutcome()
        if parsed.events:
            payload = IngestPayload(source=source, events=parsed.events)
            outcome = await self._deliver_payload(payload, import_config)

        # Phase 3 -- commit under the lock, unless the store moved.
        async with self._lock:
            committed = epoch == self._store_epoch
            stored_records: list[StoredEventRecord] = []
            if parsed.events:
                stored_records = _build_stored_records(
                    events=parsed.events,
                    lineages=parsed.parent_lot_codes,
                    source=source,
                    delivery_mode=delivery.mode,
                    outcome=outcome,
                )
                if committed:
                    self.store.add_many(stored_records)
                else:
                    logger.warning(
                        "Dropped a CSV import commit: the store changed during delivery: "
                        "tenant=%s events=%d",
                        self.tenant_id,
                        len(parsed.events),
                    )
=======

        # Parsing is CPU-bound over a caller-supplied body (up to
        # MAX_CSV_IMPORT_CHARS), so it runs on a worker thread -- and outside
        # `_lock`, which it used to be awaited inside. Nothing it reads is
        # controller state, so there is no reason for a 4 MiB parse to sit in
        # front of every other operation for this tenant.
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
            # Delivery runs without the controller lock held, and in slices
            # RegEngine will accept: a single payload above the 500-event cap
            # used to 422 and lose the entire import (#103).
            chunk_outcomes: list[DeliveryOutcome] = []
            offset = 0
            for chunk in chunk_events(parsed.events):
                # An import is all-or-nothing against one log: once the log it
                # was aimed at is gone, sending the rest of the rows into a
                # store that will not keep them is pure waste.
                if self._store_moved(epoch):
                    break
                payload = IngestPayload(source=source, events=chunk)
                chunk_outcome = await self._deliver_payload(payload, import_config)
                chunk_outcomes.append(chunk_outcome)
                # Records are built from the chunk that carried them, so each
                # keeps its own idempotency key and its own per-event verdict.
                stored_records.extend(
                    build_stored_records(
                        source=source,
                        events=chunk,
                        parent_lot_codes=parsed.parent_lot_codes[offset : offset + len(chunk)],
                        delivery_mode=delivery.mode,
                        outcome=chunk_outcome,
                    )
                )
                offset += len(chunk)
            outcome = aggregate_outcomes(chunk_outcomes)
            async with self._lock:
                stale = self._store_moved(epoch)
                if stale:
                    # Nothing is written back: the import is reported as not
                    # landed (`stored=0`) rather than half-appended to a log
                    # the operator just cleared.
                    stored_records = []
                else:
                    await self._persist_new_records(stored_records)
>>>>>>> origin/main

        async with self._lock:
            rejected = parsed.total - len(parsed.events)
<<<<<<< HEAD
            status: Literal["accepted", "partial", "rejected", "delivery_failed"]
            if outcome.delivery_status == "failed":
=======
            if stale:
                status = "delivery_failed"
            elif outcome.delivery_status == "failed":
>>>>>>> origin/main
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
<<<<<<< HEAD
                error=_with_stale_note(outcome.error_message, committed),
=======
                error=STALE_COMMIT_ERROR if stale else outcome.error_message,
>>>>>>> origin/main
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

        # Stop-then-reconfigure is one atomic critical section so a concurrent
        # start() cannot slip a run loop in between the two halves.
        async with self._lifecycle_lock:
            await self._stop_run_loop()
            async with self._lock:
                source = request.source or self.config.source
                delivery = merge_delivery_credentials(self.config.delivery, request.delivery)
                validate_live_delivery(delivery)
                self.config = self.config.model_copy(
                    update={
                        "source": source,
                        "scenario": fixture.scenario,
                        "delivery": delivery,
                    },
                    deep=True,
                )
                fixture_config = self.config.model_copy(deep=True)
                await self._configure_store(self.config.persist_path)
                self.engine.reset(self.config.seed, scenario=fixture.scenario, scale=self.config.scale)
                if request.reset:
                    self.store.reset()
                    self.mock_service.reset()
                # This *is* a log swap for anyone else mid-flight (engine
                # reset, and optionally a cleared store), so their batches go
                # stale here; this load's own snapshot is taken after the bump.
                self._invalidate_in_flight_deliveries()
                epoch = self._store_epoch

        events =[fixture_event.event for fixture_event in fixture.events]
        # Delivery runs without the controller lock held.
        payload = IngestPayload(source=source, events=events)
        outcome = await self._deliver_payload(payload, fixture_config)

        stored_records = build_stored_records(
            source=source,
            events=events,
            parent_lot_codes=[
                list(fixture_event.parent_lot_codes) for fixture_event in fixture.events
            ],
            delivery_mode=delivery.mode,
            outcome=outcome,
        )

        # Phase 1 -- reconfigure and snapshot under the lock. The epoch is read
        # *after* this path's own reset, so the fixture load does not treat its
        # own clearing of the store as somebody else's.
        async with self._lock:
<<<<<<< HEAD
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
            self.store.configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=fixture.scenario, scale=self.config.scale)
            if request.reset:
                self.store.reset()
                self.mock_service.reset()
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
            stored_records = _build_stored_records(
                events=events,
                lineages=[list(fixture_event.parent_lot_codes) for fixture_event in fixture.events],
                source=source,
                delivery_mode=delivery.mode,
                outcome=outcome,
            )
            if committed:
                self.store.add_many(stored_records)
            else:
                logger.warning(
                    "Dropped a fixture-load commit: the store changed during delivery: "
                    "tenant=%s fixture=%s events=%d",
                    self.tenant_id,
                    fixture.id.value,
                    len(events),
                )

            fixture_status: Literal["loaded", "delivery_failed"]
            if outcome.delivery_status == "failed":
                fixture_status = "delivery_failed"
            elif not committed:
                # A load that stored nothing must not answer `loaded`; #217
                # records exactly this shape as a defect in the other tree.
                fixture_status = "delivery_failed"
            else:
                fixture_status = "loaded"

            result = DemoFixtureLoadResponse(
                status=fixture_status,
=======
            # The fixture is delivered after `_lifecycle_lock` is released, so
            # a reset (or a second fixture load) can land in between. The
            # fixture is a *replacement* for the log's contents, so a load that
            # can no longer own the log is dropped whole rather than mixed into
            # whatever replaced it.
            stale = self._store_moved(epoch)
            if not stale:
                await self._persist_new_records(stored_records)
            else:
                stored_records = []
            result = DemoFixtureLoadResponse(
                status="delivery_failed"
                if stale or outcome.delivery_status == "failed"
                else "loaded",
>>>>>>> origin/main
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
<<<<<<< HEAD
                error=_with_stale_note(outcome.error_message, committed),
=======
                error=STALE_COMMIT_ERROR if stale else outcome.error_message,
>>>>>>> origin/main
            )
        await self._publish_update()
        return result

    async def list_scenario_saves(self) -> ScenarioSaveListResponse:
<<<<<<< HEAD
        # `list()` reads and JSON-parses one file per scenario. On the event
        # loop that is a stall on a plain listing, so it goes to a thread. There
        # is no lock here: listing takes no consistent view across saves.
=======
>>>>>>> origin/main
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
            config = sanitize_saved_config(config)
            records = self.store.all_between()
<<<<<<< HEAD
            # Offloaded but deliberately still under `self._lock`. Hoisting the
            # write out would let a `reset()` land between snapshotting
            # `records` above and writing them here, so the save would persist a
            # store state that never existed. The lock is held across an await,
            # which is what the delivery paths must not do -- but this await is
            # local disk, not the network, so it is bounded by the filesystem
            # rather than by a remote endpoint's timeout.
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
        # lock for itself.
=======
        # Write is done outside the lock so long delivery does not pin the data-plane.
        # The snapshot values were captured under the lock above.
        snapshot = await asyncio.to_thread(
            self.scenario_saves.save_snapshot,
            scenario=scenario_id,
            config=config,
            records=records,
        )
        return ScenarioSaveResponse(
            status="saved",
            save=self._scenario_save_summary(snapshot),
            config=snapshot.config,
        )

    async def load_scenario_save(self, scenario_id: ScenarioId) -> ScenarioLoadResponse:
        await self.stop()
>>>>>>> origin/main
        snapshot = await asyncio.to_thread(self.scenario_saves.get, scenario_id)
        if snapshot is None:
            raise KeyError(scenario_id.value)

        async with self._lifecycle_lock:
            await self._stop_run_loop()
            async with self._lock:
                self.config = snapshot.config
                await self._configure_store(self.config.persist_path)
                loaded_records = await asyncio.to_thread(
                    self.store.replace_all, snapshot.records
                )
                self.engine.reset(
                    self.config.seed, scenario=self.config.scenario, scale=self.config.scale
                )
                # `replace_all` above rewrote the log wholesale; anything
                # delivered against the previous contents is stale.
                self._invalidate_in_flight_deliveries()
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
        # Set in phase 1 for the three cases that deliver nothing; otherwise
        # left None so phase 2 knows there is a batch to send.
        result: DeliveryRetryResponse | None = None
        # Phase 1 -- snapshot under the lock.
        async with self._lock:
            delivery = merge_delivery_credentials(self.config.delivery, request.delivery)
            base_config = self.config.model_copy(deep=True)
            candidates = self.store.failed_delivery_records(request.record_ids, limit=request.limit)
            requested = len(request.record_ids) if request.record_ids else len(candidates)
            skipped = max(0, requested - len(candidates))
            epoch = self._store_epoch

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
            validate_live_delivery(delivery)
            posted = 0
            failed = 0
            attempted = 0
            updated_records: list[StoredEventRecord] = []
            responses: list[dict[str, Any]] = []
            grouped_records: dict[tuple[str, str | None], list[StoredEventRecord]] = {}
            for record in candidates:
                source = request.source or record.payload_source
                idempotency_key = (
                    retry_idempotency_key(record)
                    if delivery.mode != DestinationMode.NONE
                    else None
                )
                grouped_records.setdefault((source, idempotency_key), []).append(record)

            # Deliveries run without the controller lock held: a multi-group
            # retry against a slow endpoint must not queue up operator actions.
            for (source, idempotency_key), records in grouped_records.items():
                # `candidates` are records read out of a log that may since
                # have been cleared or swapped. Stop before spending another
                # request on a record whose row no longer exists.
                if self._store_moved(epoch):
                    break
                retry_config = base_config.model_copy(
                    update={
                        "source": source,
                        "delivery": delivery,
                    },
                    deep=True,
                )
                payload = IngestPayload(source=source, events=[record.event for record in records])
                outcome = await self._deliver_payload(
                    payload,
                    retry_config,
                    idempotency_key=idempotency_key,
                )
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

                updated_records.extend(
                    rebuild_retried_records(
                        records=records,
                        delivery_mode=delivery.mode,
                        outcome=outcome,
                    )
                )
                posted += outcome.posted
                failed += outcome.failed
                attempted += len(records)

            async with self._lock:
                # `update_many` rewrites the whole persisted log from the
                # records currently in it, so committing across a reset would
                # write a log the operator had just emptied. The retry is
                # abandoned instead: nothing is written back, the records keep
                # their `failed` status, and retrying again is safe -- a
                # verdict-less attempt reuses its original idempotency key
                # (#95), so an attempt that did reach RegEngine cannot be
                # double-ingested by the next click.
                stale = self._store_moved(epoch)
                if not stale:
                    # Off the event loop; the lock stays held so the
                    # check above and this write are one atomic step.
                    await asyncio.to_thread(self.store.update_many, updated_records)
            if stale:
                status = "skipped"
            elif posted and failed:
                status = "partial"
            elif failed:
                status = "failed"
            else:
<<<<<<< HEAD
                _validate_live_delivery(delivery)
                retry_config_base = self.config
                epoch = self._store_epoch
                grouped_records: dict[tuple[str, str | None], list[StoredEventRecord]] = {}
                for record in candidates:
                    source = request.source or record.payload_source
                    idempotency_key = (
                        _stored_idempotency_key(record)
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
                outcome = await self._deliver_payload(
                    payload,
                    retry_config,
                    idempotency_key=idempotency_key,
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

            # Phase 3 -- commit under the lock, unless the store moved. This one
            # matters most: `update_many` rewrites the whole log, so applying it
            # after a `reset()` would resurrect every record the operator just
            # deleted.
            async with self._lock:
                committed = epoch == self._store_epoch
                if committed:
                    self.store.update_many(updated_records)
                else:
                    logger.warning(
                        "Dropped a delivery-retry commit: the store changed during delivery: "
                        "tenant=%s records=%d",
                        self.tenant_id,
                        len(updated_records),
                    )
                retry_status: Literal["empty", "posted", "partial", "failed", "skipped"]
                if posted and failed:
                    retry_status = "partial"
                elif failed:
                    retry_status = "failed"
                else:
                    retry_status = "posted"
                result = DeliveryRetryResponse(
                    status=retry_status,
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
=======
                status = "posted"
            result = DeliveryRetryResponse(
                status=status,
                requested=requested,
                retryable=len(candidates),
                attempted=attempted,
                posted=posted,
                failed=failed,
                skipped=skipped + (len(candidates) - attempted),
                delivery_mode=delivery.mode,
                record_ids=[record.record_id for record in candidates],
                responses=responses,
                error=STALE_COMMIT_ERROR
                if stale
                else next((item["error"] for item in responses if item.get("error")), None),
            )
>>>>>>> origin/main
        await self._publish_update()
        return result

    async def _configure_store(self, persist_path: str) -> None:
        """Point the store at `persist_path` without blocking the event loop.

        `EventStore.configure` reloads the whole log from disk -- an open, a
        read, and a `model_validate_json` per line -- so on a volume-backed
        deployment with tens of thousands of records it is real work, and it
        ran inline on the single event loop from `start()`, `reset()`,
        `load_demo_fixture()` and `load_scenario_save()`. Every *write* path
        was already offloaded (`add_many`, `update_many`, `replace_all`); this
        is the read half of the same rule. `EventStore` guards its own state
        with a threading RLock, so a worker thread is safe.

        Awaited while `_lock` is held, deliberately: the reload is part of the
        atomic reconfigure, and unlike a delivery it is bounded by local disk
        rather than by a remote endpoint's timeout. The loop stays free either
        way, which is the point.
        """
        await asyncio.to_thread(self.store.configure, persist_path)

    async def _persist_new_records(self, records: list[StoredEventRecord]) -> None:
        """Append records to the store without blocking the event loop.

        `EventStore.add_many` fsyncs the batch before exposing it in memory
        and rolls back a partial write, so it must keep running exactly as
        written -- it is simply moved onto a worker thread. Durability is
        the store's job; keeping the single event loop responsive is ours.
        """
        await asyncio.to_thread(self.store.add_many, records)

    async def _deliver_payload(
        self,
        payload: IngestPayload,
        config: SimulationConfig,
        idempotency_key: str | None = None,
    ) -> DeliveryOutcome:
        """Send one payload to the configured destination.

        Thin delegation to `app.delivery.deliver_payload`: the mechanics of
        talking to mock/live and turning success or failure into a
        `DeliveryOutcome` are not controller state. Kept as a method because
        it is the seam tests replace to simulate slow or failing delivery.
        """
        return await deliver_payload(
            payload,
            config,
            mock_service=self.mock_service,
            live_client=self.live_client,
            idempotency_key=idempotency_key,
        )

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        """Generate and deliver batches until stopped -- or until it can't.

        The loop used to have no handler at all, which made a crash the
        quietest failure in the app. If `step()` raised -- a full disk, a
        persist volume that went away, an unexpected engine error -- the task
        died, `running` flipped to False, and *nothing else happened*: no log
        line except asyncio's "Task exception was never retrieved" whenever the
        task was eventually collected, and no `_publish_update()`, so
        `_revision` never moved. `/api/simulate/stream` blocks in
        `wait_for_revision` until the revision changes, so the console kept
        rendering its last snapshot -- "Running", the last event count, the
        last audit verdict -- indefinitely. An operator mid-demo watched a
        healthy-looking line that had silently stopped producing and
        persisting events.

        So the crash is now recorded (`status()` reports it as
        `stats.run_error`), logged with a traceback, and -- in `finally`,
        because a clean stop needs it too -- published, which forces a fresh
        snapshot down every open stream.
        """
        try:
<<<<<<< HEAD
            if config.delivery.mode == DestinationMode.MOCK:
                response = self.mock_service.ingest(
                    payload,
                    idempotency_key=delivery_idempotency_key,
                    friction=tuple(config.delivery.mock_friction),
                ).model_dump(mode="json")
                return DeliveryOutcome(
                    response=mask_secret_in_payload(response, api_key),
                    delivery_status="posted",
                    posted=response.get("accepted", len(payload.events)),
                    failed=response.get("rejected", 0),
                    delivery_attempts=1,
                    attempted_at=attempted_at,
                    completed_at=datetime.now(UTC),
                    metadata={
                        "delivery_mode": "mock",
                        "attempted_event_count": len(payload.events),
                        "idempotency_key": delivery_idempotency_key,
                    },
                )
            if config.delivery.mode == DestinationMode.LIVE:
                result = await self.live_client.ingest(
                    payload,
                    config,
                    idempotency_key=delivery_idempotency_key,
                )
                return DeliveryOutcome(
                    response=mask_secret_in_payload(result.response, api_key),
                    delivery_status="posted",
                    posted=result.response.get("accepted", len(payload.events)),
                    failed=result.response.get("rejected", 0),
                    delivery_attempts=1,
                    attempted_at=attempted_at,
                    completed_at=datetime.now(UTC),
                    metadata=result.metadata | {"attempted_event_count": len(payload.events)},
                )
            return DeliveryOutcome()
        except MockRegEngineHTTPError as exc:
            self._log_delivery_failure("mock", delivery_idempotency_key, payload, exc)
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
            self._log_delivery_failure("live", delivery_idempotency_key, payload, exc)
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
            self._log_delivery_failure(
                config.delivery.mode.value, delivery_idempotency_key, payload, exc, unexpected=True
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

    def _log_delivery_failure(
        self,
        delivery_mode: str,
        idempotency_key: str | None,
        payload: IngestPayload,
        exc: Exception,
        unexpected: bool = False,
    ) -> None:
        """Put a delivery failure in the log stream, not only in the response.

        Before this, a failed delivery left no trace anywhere an operator
        watching `docker logs` or Railway would see it -- the only record was
        the value returned to whoever happened to be polling, or the `error`
        field on the stored record. `idempotency_key` is the correlation id
        because it already identifies the attempt end to end.

        The message is masked with the same helper the response uses, so an api
        key echoed back inside an error string does not reach the log unmasked.
        """
        logger.error(
            "Delivery failed: tenant=%s mode=%s idempotency_key=%s events=%d error=%s",
            self.tenant_id,
            delivery_mode,
            idempotency_key,
            len(payload.events),
            mask_secret_in_string(str(exc), self.config.delivery.api_key),
            exc_info=unexpected,
        )

    async def _run_loop(self) -> None:
        """Drive the simulation until stopped.

        A crash in here used to be invisible. The task ended, `running` went
        False because `task.done()` was already true, and `stop()` therefore
        early-returned without clearing `self._task` or bumping the revision
        -- so the SSE stream never told the console the run had died, and
        asyncio logged a "never retrieved" exception at GC time instead.
        """
        try:
            while not self._stop_event.is_set():
=======
            while not stop_event.is_set():
>>>>>>> origin/main
                await self.step(self.config.batch_size)
                if self.config.interval_seconds <= 0:
                    await asyncio.sleep(0)
                else:
                    try:
<<<<<<< HEAD
                        await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.interval_seconds)
                    except TimeoutError:
                        continue
        except asyncio.CancelledError:
            # Shutdown cancels a wedged loop; that is not a run failure, and
            # publishing here would await inside a cancellation.
            raise
        except Exception:
            logger.exception(
                "Simulation run loop stopped on an unhandled error: tenant=%s",
                self.tenant_id,
            )
            # The run is over either way -- tell subscribers, so the console
            # stops showing it as running. Deliberately not re-raised: the
            # failure is already logged with its traceback and published, and
            # re-raising would leave an unretrieved exception on a task that
            # nothing is guaranteed to await.
=======
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=self.config.interval_seconds
                        )
                    except asyncio.TimeoutError:
                        continue
        except asyncio.CancelledError:
            # A bounded `shutdown()` cancels the loop on purpose; that is not
            # a failure to report.
            raise
        except Exception as exc:
            self._last_run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Simulation run loop stopped after an unhandled error")
        finally:
>>>>>>> origin/main
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
        return {
            "running": self.running,
            "config": sanitize_public_config(self.config).model_dump(mode="json"),
            "stats": {
                # `StatusResponse.stats` is a free-form dict, which is why the
                # run error rides here rather than as a sibling of `running`:
                # a top-level key would be dropped by the response model and
                # reach the SSE snapshot only, so the console would disagree
                # with `/api/simulate/status` about why a run stopped.
                "run_error": self._last_run_error,
                **self.store.stats(),
                "audit": summarize_scenario_audit(records, scenario),
                "engine": self.engine.snapshot(),
            },
        }

    def integration_status(self) -> IntegrationStatusResponse:
        return build_integration_status(self.config.delivery)

    async def configure_integration(
        self, request: IntegrationConfigureRequest
    ) -> IntegrationStatusResponse:
        async with self._lock:
            delivery = apply_integration_updates(self.config.delivery, request)
            validate_live_delivery(delivery)
            self.config = self.config.model_copy(update={"delivery": delivery}, deep=True)
        await self._publish_update()
        return self.integration_status()

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
<<<<<<< HEAD


def _with_stale_note(error_message: str | None, committed: bool) -> str | None:
    """Fold the dropped-commit note into a response's `error` field.

    A caller has to be able to tell "your events were delivered and stored"
    from "your events were delivered and then thrown away", and the machine-
    readable status alone cannot say it -- the delivery genuinely succeeded.
    """
    if committed:
        return error_message
    if error_message:
        return f"{error_message} {STALE_COMMIT_NOTE}"
    return STALE_COMMIT_NOTE


def _build_stored_records(
    events: list[RegEngineEvent],
    lineages: list[list[str]],
    source: str,
    delivery_mode: DestinationMode,
    outcome: DeliveryOutcome,
) -> list[StoredEventRecord]:
    """Turn a delivered batch into the records that describe it.

    Shared by `step`, `import_csv` and `load_demo_fixture`, which built this
    identically three times over.
    """
    response_events = (outcome.response or {}).get("events", []) if outcome.response else []
    paired_responses = _pair_event_responses(events, response_events)
    return [
        StoredEventRecord(
            payload_source=source,
            event=event,
            parent_lot_codes=lineages[index],
            destination_mode=delivery_mode,
            delivery_attempts=outcome.delivery_attempts,
            last_delivery_attempt_at=outcome.attempted_at,
            delivery_response=paired_responses[index],
            delivery_metadata=outcome.metadata,
            **_event_delivery_fields(outcome, paired_responses[index]),
        )
        for index, event in enumerate(events)
    ]


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


def _validate_live_delivery(delivery: DeliveryConfig) -> None:
    if delivery.mode == DestinationMode.LIVE and (not delivery.api_key or not delivery.tenant_id):
        raise ValueError("Live delivery requires both api_key and tenant_id")


def _stored_idempotency_key(record: StoredEventRecord) -> str | None:
    metadata = record.delivery_metadata or {}
    value = metadata.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None
=======
>>>>>>> origin/main

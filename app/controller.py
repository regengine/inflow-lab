from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

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
from .mock_service import MockRegEngineService
from .schemas.domain import DemoFixtureId, DestinationMode, StoredEventRecord
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
from .schemas.simulation import SimulationConfig, StepResponse
from .regengine_client import LiveRegEngineClient
from .runtime_guard import MultiProcessRuntimeError, enforce_single_process_runtime
from .schemas.integration import IntegrationConfigureRequest, IntegrationStatusResponse
from .scenario_saves import ScenarioSaveStore
from .scenarios import ScenarioId, get_scenario
from .store import EventStore


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


class SimulationController:
    def __init__(
        self,
        engine: LegitFlowEngine,
        store: EventStore,
        scenario_saves: ScenarioSaveStore,
        mock_service: MockRegEngineService,
        live_client: LiveRegEngineClient,
    ) -> None:
        # Checked here rather than in an entrypoint so it holds for every way
        # the app is started -- uvicorn, gunicorn, a script, a test harness.
        enforce_single_process_runtime()
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
        # Guards run-loop lifecycle (`_task`/`_stop_event`) and keeps
        # stop-then-reconfigure sequences atomic against a concurrent start().
        # The run loop itself never takes this lock, so it is always safe to
        # await the loop task while holding it.
        self._lifecycle_lock = asyncio.Lock()
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
                    await task
            self._task = None

    async def reset(self, config: SimulationConfig | None = None) -> None:
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
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        async with self._lock:
            validate_live_delivery(self.config.delivery)
            step_config = self.config.model_copy(deep=True)
            epoch = self._store_epoch
            size = batch_size or step_config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

        # Delivery can block on the network for the full live timeout, so it
        # happens outside the controller lock — start/stop/status stay responsive.
        payload = IngestPayload(source=step_config.source, events=events)
        outcome = await self._deliver_payload(
            payload,
            step_config,
            idempotency_key=uuid.uuid4().hex,
        )

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
                error=STALE_COMMIT_ERROR if stale else outcome.error_message,
            )
        await self._publish_update()
        return result

    async def replay(self, request: ReplayRequest | None = None) -> ReplayResponse:
        request = request or ReplayRequest()
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
                posted=outcome.posted,
                failed=outcome.failed,
                source=source,
                persist_path=persist_path,
                delivery_mode=delivery.mode,
                delivery_attempts=outcome.delivery_attempts,
                response=outcome.response,
                error=STALE_DELIVERY_ABORTED_ERROR if abandoned else outcome.error_message,
            )
        await self._publish_update()
        return result

    async def import_csv(self, request: CSVImportRequest) -> CSVImportResponse:
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

        async with self._lock:
            rejected = parsed.total - len(parsed.events)
            if stale:
                status = "delivery_failed"
            elif outcome.delivery_status == "failed":
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
                error=STALE_COMMIT_ERROR if stale else outcome.error_message,
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

        async with self._lock:
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
                error=STALE_COMMIT_ERROR if stale else outcome.error_message,
            )
        await self._publish_update()
        return result

    async def list_scenario_saves(self) -> ScenarioSaveListResponse:
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
            while not stop_event.is_set():
                await self.step(self.config.batch_size)
                if self.config.interval_seconds <= 0:
                    await asyncio.sleep(0)
                else:
                    try:
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

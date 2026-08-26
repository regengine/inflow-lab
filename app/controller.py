from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import urlparse

from .audit import summarize_scenario_audit
from .contract import INFLOW_CONTRACT_VERSION
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


logger = logging.getLogger(__name__)


# Simulation run state -- `running`, `_task`, `_stop_event` -- and the tenant
# controller registry in `tenancy.py` live in one process's memory. There is no
# shared coordination point, so under two or more workers each request lands on
# an arbitrary process: a Stop can return 200 while a *different* worker's run
# loop keeps generating and delivering events, including live traffic.
#
# Single-process is therefore a hard requirement, not a coincidence of the
# current Dockerfile CMD happening to omit `--workers`. Rather than leave it as
# an accident that a later `WEB_CONCURRENCY=4` or a Railway replica bump would
# silently break, the invariant is enforced here: a controller refuses to exist
# in a process that was told to run alongside siblings. See
# DEPLOYMENT_PROFILES.md ("Single-process requirement").
#
# Moving run/stop state to a shared coordination point (so Stop works from any
# worker) is the larger fix and remains open; this makes the current limit
# loud instead of silent.
WORKER_COUNT_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")
REPLICA_COUNT_ENV_VARS = ("RAILWAY_REPLICA_COUNT", "WEB_REPLICAS")


class MultiProcessRuntimeError(RuntimeError):
    """Raised when the process is configured to run alongside sibling workers."""


def _configured_process_count(name: str, environ: Mapping[str, str]) -> int | None:
    """Parse a worker/replica count env var, ignoring unset or malformed values."""
    raw_value = (environ.get(name) or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Ignoring malformed %s=%r when checking worker count", name, raw_value)
        return None


def enforce_single_process_runtime(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to run in a process configured for multi-worker/multi-replica.

    Only an *explicit* count above 1 trips this: an unset or malformed value
    is the single-process default and is allowed through, so the documented
    local and Railway profiles keep working untouched.
    """
    environ = os.environ if environ is None else environ
    for name in (*WORKER_COUNT_ENV_VARS, *REPLICA_COUNT_ENV_VARS):
        count = _configured_process_count(name, environ)
        if count is not None and count > 1:
            raise MultiProcessRuntimeError(
                f"{name}={count} would run the simulator in more than one process, but "
                "simulation run/stop state is per-process: a Stop request could return "
                "200 while another process keeps delivering events. Run a single worker "
                "and a single replica (see DEPLOYMENT_PROFILES.md)."
            )


# Delivery mechanics now live in ``app.delivery``. These names stay importable
# from ``app.controller`` because tests and older callers reach for them here.
_pair_event_responses = pair_event_responses
_event_delivery_fields = event_delivery_fields

__all__ = [
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

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def revision(self) -> int:
        return self._revision

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
                config = self._preserve_delivery_credentials(config)
                _validate_live_delivery(config.delivery)
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
                self.store.configure(config.persist_path)
                if engine_changed:
                    self.engine.reset(config.seed, scenario=config.scenario, scale=config.scale)
            if not self.running:
                self._launch_run_loop()
        await self._publish_update()

    def _launch_run_loop(self) -> None:
        """Start a fresh run loop. Caller must hold `_lifecycle_lock`."""
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
            return False
        self._stop_event.set()
        await task
        return True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            stopped = await self._stop_run_loop()
        if stopped:
            await self._publish_update()

    async def shutdown(self) -> None:
        await self.stop()

    async def reset(self, config: SimulationConfig | None = None) -> None:
        async with self._lifecycle_lock:
            await self._stop_run_loop()
            async with self._lock:
                if config is not None:
                    self.config = self._preserve_delivery_credentials(config)
                self.store.configure(self.config.persist_path)
                self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
                self.store.reset()
                self.mock_service.reset()
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        async with self._lock:
            _validate_live_delivery(self.config.delivery)
            step_config = self.config.model_copy(deep=True)
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
            records = self.store.read_persisted_records(persist_path)
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
            # Delivery happens outside the controller lock; a slow live
            # endpoint must not block start/stop/status. Replaying a store
            # that has grown past the 500-event batch cap is the steady state
            # on a volume-backed deployment, so the read is delivered in
            # slices rather than as one oversized payload (#103).
            chunk_outcomes = []
            for chunk in chunk_events(events):
                chunk_payload = IngestPayload(source=source, events=chunk)
                chunk_outcomes.append(await self._deliver_payload(chunk_payload, replay_config))
            outcome = aggregate_outcomes(chunk_outcomes)
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
            # Parsing is CPU-bound over a caller-supplied body, so it runs on a
            # worker thread rather than stalling the single event loop.
            parsed = await asyncio.to_thread(
                parse_csv_import,
                request.import_type,
                request.csv_text,
                default_timestamp=datetime.now(UTC),
            )

        outcome = DeliveryOutcome()
        stored_records: list[StoredEventRecord] = []
        if parsed.events:
            # Delivery runs without the controller lock held, and in slices
            # RegEngine will accept: a single payload above the 500-event cap
            # used to 422 and lose the entire import (#103).
            chunk_outcomes: list[DeliveryOutcome] = []
            offset = 0
            for chunk in chunk_events(parsed.events):
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
                await self._persist_new_records(stored_records)

        async with self._lock:
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

        # Stop-then-reconfigure is one atomic critical section so a concurrent
        # start() cannot slip a run loop in between the two halves.
        async with self._lifecycle_lock:
            await self._stop_run_loop()
            async with self._lock:
                source = request.source or self.config.source
                delivery = self._merge_delivery_credentials(request.delivery)
                _validate_live_delivery(delivery)
                self.config = self.config.model_copy(
                    update={
                        "source": source,
                        "scenario": fixture.scenario,
                        "delivery": delivery,
                    },
                    deep=True,
                )
                fixture_config = self.config.model_copy(deep=True)
                self.store.configure(self.config.persist_path)
                self.engine.reset(self.config.seed, scenario=fixture.scenario, scale=self.config.scale)
                if request.reset:
                    self.store.reset()
                    self.mock_service.reset()

        events = [fixture_event.event for fixture_event in fixture.events]
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
            await self._persist_new_records(stored_records)
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
        snapshot = self.scenario_saves.get(scenario_id)
        if snapshot is None:
            raise KeyError(scenario_id.value)

        async with self._lifecycle_lock:
            await self._stop_run_loop()
            async with self._lock:
                self.config = snapshot.config
                self.store.configure(self.config.persist_path)
                loaded_records = await asyncio.to_thread(
                    self.store.replace_all, snapshot.records
                )
                self.engine.reset(
                    self.config.seed, scenario=self.config.scenario, scale=self.config.scale
                )
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
            delivery = self._merge_delivery_credentials(request.delivery)
            base_config = self.config.model_copy(deep=True)
            candidates = self.store.failed_delivery_records(request.record_ids, limit=request.limit)
            requested = len(request.record_ids) if request.record_ids else len(candidates)
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
                    _retry_idempotency_key(record)
                    if delivery.mode != DestinationMode.NONE
                    else None
                )
                grouped_records.setdefault((source, idempotency_key), []).append(record)

            # Deliveries run without the controller lock held: a multi-group
            # retry against a slow endpoint must not queue up operator actions.
            for (source, idempotency_key), records in grouped_records.items():
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

            async with self._lock:
                # Rewrites the whole persisted log; off the event loop.
                await asyncio.to_thread(self.store.update_many, updated_records)
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

    def _preserve_delivery_credentials(self, config: SimulationConfig) -> SimulationConfig:
        """Fill in credentials the caller did not send from the stored config.

        `status()` scrubs `api_key` (and, in live mode, `tenant_id`) before
        it leaves the process, so a console tab that loaded after the
        credentials were configured has never seen them and cannot echo them
        back. Treating their absence as "clear them" meant Start, Reset and
        Retry silently downgraded a live integration (#148). An omitted or
        null credential therefore keeps the stored one, exactly as
        `/api/integration/configure` already behaves; clearing one is done
        there, explicitly.
        """
        delivery = self._merge_delivery_credentials(config.delivery)
        if delivery is config.delivery:
            return config
        return config.model_copy(update={"delivery": delivery}, deep=True)

    def _merge_delivery_credentials(self, delivery: DeliveryConfig | None) -> DeliveryConfig:
        """One delivery config with the caller's fields over the stored secrets.

        Scoped to live delivery, which is the only mode that uses
        credentials. Pointing the simulator back at the sandbox is also how
        an operator stops using a live tenant, so a mock/off config is taken
        at face value and drops the stored secret rather than keeping a live
        credential alive in a config that no longer sends to it.
        """
        stored = self.config.delivery
        if delivery is None:
            return stored
        if delivery.mode != DestinationMode.LIVE:
            return delivery
        updates: dict[str, Any] = {}
        if delivery.api_key is None and stored.api_key:
            updates["api_key"] = stored.api_key
        if delivery.tenant_id is None and stored.tenant_id:
            updates["tenant_id"] = stored.tenant_id
        if not updates:
            return delivery
        return delivery.model_copy(update=updates, deep=True)

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


def _validate_live_delivery(delivery: DeliveryConfig) -> None:
    if delivery.mode == DestinationMode.LIVE and (not delivery.api_key or not delivery.tenant_id):
        raise ValueError("Live delivery requires both api_key and tenant_id")


def _retry_idempotency_key(record: StoredEventRecord) -> str | None:
    """The key a retry of `record` should carry, or None to mint a fresh one.

    A reused `Idempotency-Key` is answered from RegEngine's 24h cache
    without revalidation, so replaying the original key can never redeliver
    an event the server rejected per-event on a 2xx response -- the record
    stays `failed` however many times an operator clicks retry, and the
    cached batch's `accepted` count is folded into the retry's total (#95).

    The key is therefore reused only for the case it exists to protect: an
    attempt that produced no per-event verdict at all (a transport failure,
    where the original request may or may not have reached the server, and
    replay-safety is what stops a double ingest). A record carrying a
    verdict was answered, so its retry is a genuinely new request and gets
    a new key.
    """
    if record.delivery_response is not None:
        return None
    return _stored_idempotency_key(record)


def _stored_idempotency_key(record: StoredEventRecord) -> str | None:
    metadata = record.delivery_metadata or {}
    value = metadata.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None

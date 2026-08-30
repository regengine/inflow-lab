from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from .audit import summarize_scenario_audit
from .contract import INFLOW_CONTRACT_VERSION
from .csv_importer import parse_csv_import
from .demo_fixtures import get_demo_fixture
from .engine import LegitFlowEngine
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
from .schemas.simulation import (
    DeliveryConfig,
    SimulationConfig,
    StepResponse,
)
from .store import EventStore, mask_secret_in_payload, mask_secret_in_string

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
        # Only used to identify this controller in log lines. The default
        # controller serves the shared store, so it is labelled as such rather
        # than left blank.
        self.tenant_id = tenant_id or "default"
        self.engine = engine
        self.store = store
        self.scenario_saves = scenario_saves
        self.mock_service = mock_service
        self.live_client = live_client
        self.config = SimulationConfig(persist_path=str(store.persist_path))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        # Bumped whenever the store is cleared or repointed. Delivery now runs
        # outside `_lock` (#208), so between snapshotting what to send and
        # committing the outcome, a `reset()` or a fixture load can land. The
        # epoch is how the commit phase notices: if it changed, the batch in
        # flight describes a store that no longer exists and is dropped rather
        # than written over the new one.
        self._store_epoch = 0
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
            self.config = config
            self.store.configure(config.persist_path)
            self._store_epoch += 1
            if not self.running and (
                config.seed != previous_config.seed
                or config.scenario != previous_config.scenario
                or config.scale != previous_config.scale
            ):
                self.engine.reset(config.seed, scenario=config.scenario, scale=config.scale)
            if not self.running:
                self._stop_event = asyncio.Event()
                self._task = asyncio.create_task(self._run_loop())
        await self._publish_update()

    async def stop(self) -> None:
        if not self.running:
            return
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        await task
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
                    await task
            self._task = None

    async def reset(self, config: SimulationConfig | None = None) -> None:
        await self.stop()
        async with self._lock:
            if config is not None:
                self.config = config
            self.store.configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=self.config.scenario, scale=self.config.scale)
            self.store.reset()
            self.mock_service.reset()
            self._store_epoch += 1
        await self._publish_update()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        # Phase 1 -- snapshot under the lock. The engine advances here because
        # its state is shared; everything the delivery needs is copied out so
        # phase 2 does not read `self` at all.
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
            epoch = self._store_epoch
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

            rejected = parsed.total - len(parsed.events)
            status: Literal["accepted", "partial", "rejected", "delivery_failed"]
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

        # Phase 1 -- reconfigure and snapshot under the lock. The epoch is read
        # *after* this path's own reset, so the fixture load does not treat its
        # own clearing of the store as somebody else's.
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
        # loop that is a stall on a plain listing, so it goes to a thread. There
        # is no lock here: listing takes no consistent view across saves.
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
        snapshot = await asyncio.to_thread(self.scenario_saves.get, scenario_id)
        if snapshot is None:
            raise KeyError(scenario_id.value)

        async with self._lock:
            self.config = snapshot.config
            self.store.configure(self.config.persist_path)
            loaded_records = self.store.replace_all(snapshot.records)
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
        # Set in phase 1 for the three cases that deliver nothing; otherwise
        # left None so phase 2 knows there is a batch to send.
        result: DeliveryRetryResponse | None = None
        # Phase 1 -- snapshot under the lock.
        async with self._lock:
            delivery = request.delivery or self.config.delivery
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
        await self._publish_update()
        return result

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
        while not self._stop_event.is_set():
            await self.step(self.config.batch_size)
            if self.config.interval_seconds <= 0:
                await asyncio.sleep(0)
            else:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.interval_seconds)
                except TimeoutError:
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

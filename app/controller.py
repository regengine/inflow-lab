from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .csv_importer import parse_csv_import
from .engine import LegitFlowEngine
from .mock_service import MockRegEngineService
from .models import (
    CSVImportRequest,
    CSVImportResponse,
    DeliveryRetryRequest,
    DeliveryRetryResponse,
    DestinationMode,
    IngestPayload,
    ReplayRequest,
    ReplayResponse,
    SimulationConfig,
    StepResponse,
    StoredEventRecord,
)
from .regengine_client import LiveRegEngineClient
from .store import EventStore


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


class SimulationController:
    def __init__(
        self,
        engine: LegitFlowEngine,
        store: EventStore,
        mock_service: MockRegEngineService,
        live_client: LiveRegEngineClient,
    ) -> None:
        self.engine = engine
        self.store = store
        self.mock_service = mock_service
        self.live_client = live_client
        self.config = SimulationConfig()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
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
            previous_config = self.config
            self.config = config
            self.store.configure(config.persist_path)
            if not self.running and (config.seed != previous_config.seed or config.scenario != previous_config.scenario):
                self.engine.reset(config.seed, scenario=config.scenario)
            if not self.running:
                self._stop_event = asyncio.Event()
                self._task = asyncio.create_task(self._run_loop())
        await self._publish_update()

    async def stop(self) -> None:
        if not self.running:
            return
        self._stop_event.set()
        assert self._task is not None
        await self._task
        self._task = None
        await self._publish_update()

    async def shutdown(self) -> None:
        await self.stop()

    async def reset(self, config: SimulationConfig | None = None) -> None:
        await self.stop()
        async with self._lock:
            if config is not None:
                self.config = config
            self.store.configure(self.config.persist_path)
            self.engine.reset(self.config.seed, scenario=self.config.scenario)
            self.store.reset()
            self.mock_service.reset()
        await self._publish_update()

    async def step(self, batch_size: int | None = None) -> StepResponse:
        async with self._lock:
            size = batch_size or self.config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

            payload = IngestPayload(source=self.config.source, events=events)
            outcome = await self._deliver_payload(payload, self.config)

            stored_records: list[StoredEventRecord] = []
            response_events = (outcome.response or {}).get("events", []) if outcome.response else []
            for index, event in enumerate(events):
                event_response = response_events[index] if index < len(response_events) else None
                stored_records.append(
                    StoredEventRecord(
                        payload_source=self.config.source,
                        event=event,
                        parent_lot_codes=lineages[index],
                        destination_mode=self.config.delivery.mode,
                        delivery_status=outcome.delivery_status,
                        delivery_attempts=outcome.delivery_attempts,
                        last_delivery_attempt_at=outcome.attempted_at,
                        last_delivery_success_at=outcome.completed_at
                        if outcome.delivery_status == "posted"
                        else None,
                        delivery_response=event_response,
                        error=outcome.error_message,
                    )
                )
            self.store.add_many(stored_records)

            result = StepResponse(
                generated=len(events),
                posted=outcome.posted,
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
                payload = IngestPayload(source=source, events=events)
                outcome = await self._deliver_payload(payload, replay_config)
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
            import_config = self.config.model_copy(
                update={
                    "source": source,
                    "delivery": delivery,
                },
                deep=True,
            )
            parsed = parse_csv_import(
                request.import_type,
                request.csv_text,
                default_timestamp=datetime.now(UTC),
            )

            outcome = DeliveryOutcome()
            stored_records: list[StoredEventRecord] = []
            if parsed.events:
                payload = IngestPayload(source=source, events=parsed.events)
                outcome = await self._deliver_payload(payload, import_config)
                response_events = (outcome.response or {}).get("events", []) if outcome.response else []
                for index, event in enumerate(parsed.events):
                    event_response = response_events[index] if index < len(response_events) else None
                    stored_records.append(
                        StoredEventRecord(
                            payload_source=source,
                            event=event,
                            parent_lot_codes=parsed.parent_lot_codes[index],
                            destination_mode=delivery.mode,
                            delivery_status=outcome.delivery_status,
                            delivery_attempts=outcome.delivery_attempts,
                            last_delivery_attempt_at=outcome.attempted_at,
                            last_delivery_success_at=outcome.completed_at
                            if outcome.delivery_status == "posted"
                            else None,
                            delivery_response=event_response,
                            error=outcome.error_message,
                        )
                    )
                self.store.add_many(stored_records)

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
                response=outcome.response,
                error=outcome.error_message,
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
                posted = 0
                failed = 0
                updated_records: list[StoredEventRecord] = []
                responses: list[dict[str, Any]] = []
                grouped_records: dict[str, list[StoredEventRecord]] = {}
                for record in candidates:
                    source = request.source or record.payload_source
                    grouped_records.setdefault(source, []).append(record)

                for source, records in grouped_records.items():
                    retry_config = self.config.model_copy(
                        update={
                            "source": source,
                            "delivery": delivery,
                        },
                        deep=True,
                    )
                    payload = IngestPayload(source=source, events=[record.event for record in records])
                    outcome = await self._deliver_payload(payload, retry_config)
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

                    for index, record in enumerate(records):
                        event_response = response_events[index] if index < len(response_events) else None
                        next_attempts = record.delivery_attempts + outcome.delivery_attempts
                        updated_records.append(
                            record.model_copy(
                                update={
                                    "destination_mode": delivery.mode,
                                    "delivery_status": outcome.delivery_status,
                                    "delivery_attempts": next_attempts,
                                    "last_delivery_attempt_at": outcome.attempted_at
                                    or record.last_delivery_attempt_at,
                                    "last_delivery_success_at": outcome.completed_at
                                    if outcome.delivery_status == "posted"
                                    else record.last_delivery_success_at,
                                    "delivery_response": event_response,
                                    "error": None
                                    if outcome.delivery_status == "posted"
                                    else outcome.error_message,
                                },
                                deep=True,
                            )
                        )
                    posted += outcome.posted
                    failed += outcome.failed

                self.store.update_many(updated_records)
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

    async def _deliver_payload(self, payload: IngestPayload, config: SimulationConfig) -> DeliveryOutcome:
        if config.delivery.mode == DestinationMode.NONE:
            return DeliveryOutcome()

        attempted_at = datetime.now(UTC)
        try:
            if config.delivery.mode == DestinationMode.MOCK:
                response = self.mock_service.ingest(payload).model_dump(mode="json")
                return DeliveryOutcome(
                    response=response,
                    delivery_status="posted",
                    posted=len(payload.events),
                    delivery_attempts=1,
                    attempted_at=attempted_at,
                    completed_at=datetime.now(UTC),
                )
            if config.delivery.mode == DestinationMode.LIVE:
                response = await self.live_client.ingest(payload, config)
                return DeliveryOutcome(
                    response=response,
                    delivery_status="posted",
                    posted=len(payload.events),
                    delivery_attempts=1,
                    attempted_at=attempted_at,
                    completed_at=datetime.now(UTC),
                )
            return DeliveryOutcome()
        except Exception as exc:  # pragma: no cover - exercised by live integration, not unit tests
            return DeliveryOutcome(
                delivery_status="failed",
                failed=len(payload.events),
                delivery_attempts=1,
                attempted_at=attempted_at,
                error_message=str(exc),
            )

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.step(self.config.batch_size)
            if self.config.interval_seconds <= 0:
                await asyncio.sleep(0)
            else:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.interval_seconds)
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
        return {
            "running": self.running,
            "config": self.config.model_dump(mode="json"),
            "stats": {
                **self.store.stats(),
                "engine": self.engine.snapshot(),
            },
        }

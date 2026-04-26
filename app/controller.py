from __future__ import annotations

import asyncio
from typing import Any

from .engine import LegitFlowEngine
from .mock_service import MockRegEngineService
from .models import (
    DestinationMode,
    IngestPayload,
    SimulationConfig,
    StepResponse,
    StoredEventRecord,
)
from .regengine_client import LiveRegEngineClient
from .store import EventStore


MASKED_SECRET = "***MASKED***"
SECRET_FIELD_NAMES = {"api_key", "apikey", "x_regengine_api_key", "authorization"}


def _mask_secrets(value: Any, secret: str | None = None) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower().replace("-", "_") in SECRET_FIELD_NAMES:
                masked[key] = MASKED_SECRET
            else:
                masked[key] = _mask_secrets(item, secret)
        return masked
    if isinstance(value, list):
        return [_mask_secrets(item, secret) for item in value]
    if isinstance(value, str) and secret and secret in value:
        return value.replace(secret, MASKED_SECRET)
    return value


def _mask_error(message: str, secret: str | None = None) -> str:
    if secret:
        return message.replace(secret, MASKED_SECRET)
    return message


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

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, config: SimulationConfig) -> None:
        self._validate_live_delivery(config)
        self.config = config
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self.running:
            return
        self._stop_event.set()
        assert self._task is not None
        await self._task
        self._task = None

    async def shutdown(self) -> None:
        await self.stop()

    async def reset(self, config: SimulationConfig | None = None) -> None:
        await self.stop()
        self.config = config or SimulationConfig()
        self.engine.reset(self.config.seed)
        self.store.reset()
        self.mock_service.reset()

    async def step(self, batch_size: int | None = None, config: SimulationConfig | None = None) -> StepResponse:
        async with self._lock:
            if config is not None:
                self._validate_live_delivery(config)
                self.config = config
            self._validate_live_delivery(self.config)
            size = batch_size or self.config.batch_size
            events = []
            lineages = []
            for _ in range(size):
                event, parent_lot_codes = self.engine.next_event()
                events.append(event)
                lineages.append(parent_lot_codes)

            payload = IngestPayload(source=self.config.source, events=events)
            response: dict[str, Any] | None = None
            error_message: str | None = None
            delivery_statuses = ["generated"] * len(events)

            try:
                if self.config.delivery.mode == DestinationMode.MOCK:
                    response = _mask_secrets(
                        self.mock_service.ingest(payload).model_dump(mode="json"),
                        self.config.delivery.api_key,
                    )
                    delivery_statuses = self._statuses_from_response(response, len(events))
                elif self.config.delivery.mode == DestinationMode.LIVE:
                    response = _mask_secrets(
                        await self.live_client.ingest(payload, self.config),
                        self.config.delivery.api_key,
                    )
                    delivery_statuses = self._statuses_from_response(response, len(events))
            except Exception as exc:  # pragma: no cover - exercised by live integration, not unit tests
                error_message = _mask_error(str(exc), self.config.delivery.api_key)
                delivery_statuses = ["failed"] * len(events)

            stored_records: list[StoredEventRecord] = []
            response_events = (response or {}).get("events", []) if response else []
            for index, event in enumerate(events):
                event_response = response_events[index] if index < len(response_events) else None
                stored_records.append(
                    StoredEventRecord(
                        payload_source=self.config.source,
                        event=event,
                        parent_lot_codes=lineages[index],
                        destination_mode=self.config.delivery.mode,
                        delivery_status=delivery_statuses[index],
                        delivery_response=event_response,
                        error=error_message,
                    )
                )
            self.store.add_many(stored_records)

            accepted = delivery_statuses.count("accepted")
            rejected = delivery_statuses.count("rejected")
            failed = delivery_statuses.count("failed")
            posted = accepted + rejected + delivery_statuses.count("posted")
            return StepResponse(
                generated=len(events),
                posted=posted,
                accepted=accepted,
                rejected=rejected,
                failed=failed,
                lot_codes=[event.traceability_lot_code for event in events],
                response=response,
            )

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.step(self.config.batch_size)
            if self.config.interval_seconds <= 0:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(self.config.interval_seconds)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "config": self._public_config(),
            "stats": {
                **self.store.stats(),
                "engine": self.engine.snapshot(),
            },
        }

    def _public_config(self) -> dict[str, Any]:
        config = self.config.model_dump(mode="json")
        delivery = config.get("delivery") or {}
        if delivery.get("api_key"):
            delivery["api_key"] = MASKED_SECRET
        return config

    def _validate_live_delivery(self, config: SimulationConfig) -> None:
        if config.delivery.mode != DestinationMode.LIVE:
            return

        missing: list[str] = []
        if not config.delivery.live_confirmed:
            missing.append("delivery.live_confirmed")
        if not config.delivery.endpoint:
            missing.append("delivery.endpoint")
        if not config.delivery.api_key:
            missing.append("delivery.api_key")
        if not config.delivery.tenant_id:
            missing.append("delivery.tenant_id")
        if missing:
            raise ValueError(f"Live delivery requires {', '.join(missing)}")

    @staticmethod
    def _statuses_from_response(response: dict[str, Any], event_count: int) -> list[str]:
        statuses: list[str] = []
        for event_response in response.get("events", [])[:event_count]:
            status = event_response.get("status") if isinstance(event_response, dict) else None
            statuses.append(status if status in {"accepted", "rejected", "failed"} else "posted")

        remaining = event_count - len(statuses)
        if remaining <= 0:
            return statuses[:event_count]

        accepted = int(response.get("accepted") or 0)
        rejected = int(response.get("rejected") or 0)
        failed = int(response.get("failed") or 0)
        for status, count in (("accepted", accepted), ("rejected", rejected), ("failed", failed)):
            if remaining <= 0:
                break
            add = min(count, remaining)
            statuses.extend([status] * add)
            remaining -= add

        if remaining > 0:
            statuses.extend(["posted"] * remaining)
        return statuses[:event_count]

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator


class CTEType(str, Enum):
    HARVESTING = "harvesting"
    COOLING = "cooling"
    INITIAL_PACKING = "initial_packing"
    SHIPPING = "shipping"
    RECEIVING = "receiving"
    TRANSFORMATION = "transformation"


class DestinationMode(str, Enum):
    MOCK = "mock"
    LIVE = "live"
    NONE = "none"


class RegEngineEvent(BaseModel):
    cte_type: CTEType
    traceability_lot_code: str
    product_description: str
    quantity: float
    unit_of_measure: str
    location_name: str
    timestamp: datetime
    kdes: dict[str, Any] = Field(default_factory=dict)


class IngestPayload(BaseModel):
    source: str = "codex-simulator"
    events: list[RegEngineEvent]


class DeliveryConfig(BaseModel):
    mode: DestinationMode = DestinationMode.MOCK
    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None
    live_confirmed: bool = False


class SimulationConfig(BaseModel):
    source: str = "codex-simulator"
    interval_seconds: float = 1.5
    batch_size: int = 3
    seed: int | None = 204
    persist_path: str = "data/events.jsonl"
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, value: float) -> float:
        if value < 0:
            raise ValueError("interval_seconds must be >= 0")
        return value

    @field_validator("batch_size")
    @classmethod
    def validate_batch_size(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("batch_size must be between 1 and 100")
        return value


class StoredEventRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    sequence_no: int = 0
    payload_source: str
    event: RegEngineEvent
    parent_lot_codes: list[str] = Field(default_factory=list)
    destination_mode: DestinationMode = DestinationMode.NONE
    delivery_status: Literal["generated", "posted", "accepted", "rejected", "failed"] = "generated"
    delivery_response: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IngestResponseEvent(BaseModel):
    traceability_lot_code: str
    cte_type: CTEType
    status: Literal["accepted", "rejected"]
    event_id: str
    sha256_hash: str
    chain_hash: str


class MockIngestResponse(BaseModel):
    accepted: int
    rejected: int
    total: int
    events: list[IngestResponseEvent]
    ingestion_timestamp: datetime


class StartRequest(BaseModel):
    config: SimulationConfig


class StepRequest(BaseModel):
    config: SimulationConfig | None = None


class StatusResponse(BaseModel):
    running: bool
    config: SimulationConfig
    stats: dict[str, Any]


class StepResponse(BaseModel):
    generated: int
    posted: int
    accepted: int = 0
    rejected: int = 0
    failed: int
    lot_codes: list[str]
    response: dict[str, Any] | None = None


class ResetResponse(BaseModel):
    status: str


class LineageResponse(BaseModel):
    traceability_lot_code: str
    records: list[StoredEventRecord]


class EventListResponse(BaseModel):
    events: list[StoredEventRecord]

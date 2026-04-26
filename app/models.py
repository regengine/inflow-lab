from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .scenarios import ScenarioId


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


class CSVImportType(str, Enum):
    SCHEDULED_EVENTS = "scheduled_events"
    SEED_LOTS = "seed_lots"


class FDAExportPreset(str, Enum):
    ALL_RECORDS = "all_records"
    LOT_TRACE = "lot_trace"
    SHIPMENT_HANDOFF = "shipment_handoff"
    RECEIVING_LOG = "receiving_log"
    TRANSFORMATION_BATCHES = "transformation_batches"


class DemoFixtureId(str, Enum):
    LEAFY_GREENS_TRACE = "leafy_greens_trace"
    FRESH_CUT_TRANSFORMATION = "fresh_cut_transformation"
    RETAILER_HANDOFF = "retailer_handoff"


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


class SimulationConfig(BaseModel):
    source: str = "codex-simulator"
    scenario: ScenarioId = ScenarioId.LEAFY_GREENS_SUPPLIER
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
    delivery_status: Literal["generated", "posted", "failed"] = "generated"
    delivery_attempts: int = 0
    last_delivery_attempt_at: datetime | None = None
    last_delivery_success_at: datetime | None = None
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


class StatusResponse(BaseModel):
    running: bool
    config: SimulationConfig
    stats: dict[str, Any]


class ScenarioSummary(BaseModel):
    id: ScenarioId
    label: str
    description: str


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioSummary]


class ScenarioSaveSnapshot(BaseModel):
    scenario: ScenarioId
    config: SimulationConfig
    records: list[StoredEventRecord] = Field(default_factory=list)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScenarioSaveSummary(BaseModel):
    scenario: ScenarioId
    label: str
    saved_at: datetime
    record_count: int
    lot_codes: list[str]
    source: str
    persist_path: str
    delivery_mode: DestinationMode


class ScenarioSaveListResponse(BaseModel):
    saves: list[ScenarioSaveSummary]


class ScenarioSaveRequest(BaseModel):
    config: SimulationConfig | None = None


class ScenarioSaveResponse(BaseModel):
    status: Literal["saved"]
    save: ScenarioSaveSummary
    config: SimulationConfig


class ScenarioLoadResponse(BaseModel):
    status: Literal["loaded"]
    save: ScenarioSaveSummary
    config: SimulationConfig
    loaded_records: int


class FDAExportPresetSummary(BaseModel):
    id: FDAExportPreset
    label: str
    description: str
    requires_lot_code: bool = False


class FDAExportPresetListResponse(BaseModel):
    presets: list[FDAExportPresetSummary]


class DemoFixtureSummary(BaseModel):
    id: DemoFixtureId
    label: str
    description: str
    scenario: ScenarioId
    event_count: int
    lot_codes: list[str]


class DemoFixtureListResponse(BaseModel):
    fixtures: list[DemoFixtureSummary]


class DemoFixtureLoadRequest(BaseModel):
    reset: bool = True
    source: str | None = None
    delivery: DeliveryConfig | None = None


class DemoFixtureLoadResponse(BaseModel):
    status: Literal["loaded", "delivery_failed"]
    fixture_id: DemoFixtureId
    scenario: ScenarioId
    loaded: int
    stored: int
    posted: int
    failed: int
    source: str
    delivery_mode: DestinationMode
    delivery_attempts: int = 0
    lot_codes: list[str]
    response: dict[str, Any] | None = None
    error: str | None = None


class StepResponse(BaseModel):
    generated: int
    posted: int
    failed: int
    lot_codes: list[str]
    delivery_status: Literal["generated", "posted", "failed"]
    delivery_mode: DestinationMode
    delivery_attempts: int
    response: dict[str, Any] | None = None
    error: str | None = None


class DeliveryRetryRequest(BaseModel):
    record_ids: list[str] | None = None
    limit: int = 50
    source: str | None = None
    delivery: DeliveryConfig | None = None

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        if value < 1 or value > 500:
            raise ValueError("limit must be between 1 and 500")
        return value


class DeliveryRetryResponse(BaseModel):
    status: Literal["empty", "posted", "partial", "failed", "skipped"]
    requested: int
    retryable: int
    attempted: int
    posted: int
    failed: int
    skipped: int
    delivery_mode: DestinationMode
    record_ids: list[str]
    responses: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class ReplayRequest(BaseModel):
    persist_path: str | None = None
    source: str | None = None
    delivery: DeliveryConfig | None = None


class ReplayResponse(BaseModel):
    status: Literal["empty", "posted", "rebuilt", "failed"]
    read: int
    replayed: int
    posted: int
    failed: int
    source: str
    persist_path: str
    delivery_mode: DestinationMode
    delivery_attempts: int = 0
    response: dict[str, Any] | None = None
    error: str | None = None


class CSVImportRequest(BaseModel):
    import_type: CSVImportType
    csv_text: str
    source: str | None = None
    delivery: DeliveryConfig | None = None


class CSVImportError(BaseModel):
    row: int
    field: str | None = None
    message: str


class CSVImportResponse(BaseModel):
    status: Literal["accepted", "partial", "rejected", "delivery_failed"]
    import_type: CSVImportType
    total: int
    accepted: int
    rejected: int
    stored: int
    posted: int
    failed: int
    source: str
    delivery_mode: DestinationMode
    delivery_attempts: int = 0
    lot_codes: list[str]
    errors: list[CSVImportError] = Field(default_factory=list)
    response: dict[str, Any] | None = None
    error: str | None = None


class ResetResponse(BaseModel):
    status: str


class LineageNode(BaseModel):
    lot_code: str
    product_description: str
    event_count: int
    cte_types: list[CTEType]
    first_seen: datetime
    last_seen: datetime
    locations: list[str]


class LineageEdge(BaseModel):
    source_lot_code: str
    target_lot_code: str
    cte_type: CTEType
    event_sequence_no: int


class LineageResponse(BaseModel):
    traceability_lot_code: str
    records: list[StoredEventRecord]
    nodes: list[LineageNode]
    edges: list[LineageEdge]


class EventListResponse(BaseModel):
    events: list[StoredEventRecord]

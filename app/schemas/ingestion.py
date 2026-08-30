from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import CSVImportType, CTEType, DestinationMode, RegEngineEvent
from .simulation import DeliveryConfig


class IngestPayload(BaseModel):
    # Deliberately NOT extra="forbid". This is the RegEngine wire contract, not
    # one of this app's own control-plane bodies: RegEngine's own IngestEvent
    # sets no model_config and so ignores unknown keys. Forbidding them here
    # would make the mock reject batches live ingest accepts, which is the
    # parity gap this simulator exists to close, pointing the wrong way.
    source: str = "codex-simulator"
    events: list[RegEngineEvent]


class IngestResponseEvent(BaseModel):
    # Mirrors RegEngine's EventResult: rejected events carry errors and no
    # event_id/hashes, accepted events carry all three.
    traceability_lot_code: str
    cte_type: CTEType
    status: Literal["accepted", "rejected"]
    event_id: str | None = None
    sha256_hash: str | None = None
    chain_hash: str | None = None
    errors: list[str] = Field(default_factory=list)


class MockIngestResponse(BaseModel):
    accepted: int
    rejected: int
    total: int
    events: list[IngestResponseEvent]
    ingestion_timestamp: datetime


class DeliveryRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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


# The importer parses the whole document into memory and turns every row into
# a record, with no row cap of its own, so an unbounded `csv_text` is an
# unbounded allocation. 2 MiB is roughly 13k rows at typical CTE row widths --
# far more than any demo import -- and rejects as a 422 rather than dying in
# the handler. Note this bounds what is *parsed*, not what is *received*:
# the ASGI server still buffers the request body first, so a hard byte limit
# belongs in front of the app.
MAX_CSV_TEXT_CHARS = 2 * 1024 * 1024


class CSVImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_type: CSVImportType
    csv_text: str = Field(max_length=MAX_CSV_TEXT_CHARS)
    source: str | None = None
    delivery: DeliveryConfig | None = None


class CSVImportError(BaseModel):
    row: int
    field: str | None = None
    message: str


class CSVImportWarning(BaseModel):
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
    warnings: list[CSVImportWarning] = Field(default_factory=list)
    response: dict[str, Any] | None = None
    error: str | None = None

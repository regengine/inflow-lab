from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import CSVImportType, CTEType, DestinationMode, RegEngineEvent
from .simulation import DeliveryConfig


class IngestPayload(BaseModel):
    # extra="forbid" (#143): this is a request body (POST /api/mock/regengine/
    # ingest), constructed internally elsewhere with known kwargs only. A
    # caller who misspells "events" or nests the payload under an extra
    # wrapper key would otherwise get a silent 200 that ingested nothing,
    # which is the exact failure mode this simulator exists to catch.
    model_config = ConfigDict(extra="forbid")

    source: str = "codex-simulator"
    events: list[RegEngineEvent]


class IngestResponseEvent(BaseModel):
    # Mirrors RegEngine's EventResult: rejected events carry errors and no
    # event_id/hashes, accepted events carry all three.
    #
    # Response-only model (nested in MockIngestResponse) -- left permissive
    # (no extra="forbid") on purpose. #143 targets request bodies, where an
    # unrecognized field is a caller mistake worth a 422; here it is built
    # by our own mock_service.py from known kwargs, so there is nothing to
    # guard against and forbidding would only add risk if this ever needs
    # to mirror a new field RegEngine's real response starts sending.
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
    out_of_window_events: int = 0
    event_age_window_mode: Literal["enforced", "bypassed"] | None = None
    event_age_window_days: int | None = None


class DeliveryRetryRequest(BaseModel):
    # extra="forbid" (#143): request body for POST /api/delivery/retry. A
    # typo like "record_id" (singular) would otherwise be silently dropped,
    # parsing as an empty request that retries every failed record instead
    # of the caller's intended subset -- a destructive default for a typo.
    model_config = ConfigDict(extra="forbid")

    # None ("field omitted") means "no filter -- retry every failed record
    # up to limit"; [] ("field present but empty") means "retry nothing".
    # Pydantic already keeps these distinguishable at parse time; the fix
    # for #144 is entirely in how SimulationController.retry_failed_delivery
    # consumes this value -- see app/controller.py.
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
    # extra="forbid" (#143): request body for POST /api/simulate/replay.
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


class CSVImportRequest(BaseModel):
    # extra="forbid" (#143): request body for POST /api/import/csv.
    model_config = ConfigDict(extra="forbid")

    import_type: CSVImportType
    csv_text: str
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

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import CSVImportType, CTEType, DestinationMode, RegEngineEvent
from .simulation import STRICT_REQUEST, DeliveryConfig

# RegEngine's per-batch cap. Owned here rather than in `mock_service` so the
# schema can enforce it before pydantic materialises the list; the service
# re-exports it and still checks it for direct callers.
MAX_BATCH_EVENTS = 500


class IngestPayload(BaseModel):
<<<<<<< HEAD
    # Deliberately NOT extra="forbid". This is the RegEngine wire contract, not
    # one of this app's own control-plane bodies: RegEngine's own IngestEvent
    # sets no model_config and so ignores unknown keys. Forbidding them here
    # would make the mock reject batches live ingest accepts, which is the
    # parity gap this simulator exists to close, pointing the wrong way.
    source: str = "codex-simulator"
    # Capped at the same 500 the service enforces, but here -- before pydantic
    # materialises every model. The service-layer check ran after, so a 400k
    # event batch allocated ~1.3 GB and only then raised. Matching the live
    # limit keeps the parity honest rather than making the mock stricter.
    events: list[RegEngineEvent] = Field(max_length=MAX_BATCH_EVENTS)
=======
    # Deliberately NOT strict: this is the mock stand-in for RegEngine's own
    # webhook receiver, and a receiver that 422s on an additive field a
    # newer producer sends would misrepresent live behaviour.
    source: str = "codex-simulator"
    # NOT max_length=500, deliberately -- see tests/test_schema_bounds.py.
    # RegEngine's own WebhookPayload does bound `events` at 1-500, and this
    # model is the request body for the mock's /api/mock/regengine/ingest
    # route, so a pydantic constraint here would be rejected by FastAPI as a
    # RequestValidationError whose `detail` is a list of error dicts. The mock
    # instead enforces the cap in MockRegEngineService.ingest so it can return
    # RegEngine's own wording ("events accepts at most 500 items per batch")
    # as a plain string detail, which tests/test_mock_parity.py pins. Adding
    # the constraint here changes that response body and breaks the pin.
    # Producers are already bounded: every bulk send goes through
    # `delivery.chunk_events`, which chunks at MAX_BATCH_EVENTS (#103).
    events: list[RegEngineEvent]
>>>>>>> origin/main


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


<<<<<<< HEAD
# The importer parses the whole document into memory and turns every row into
# a record, with no row cap of its own, so an unbounded `csv_text` is an
# unbounded allocation. 2 MiB is roughly 13k rows at typical CTE row widths --
# far more than any demo import -- and rejects as a 422 rather than dying in
# the handler. Note this bounds what is *parsed*, not what is *received*:
# the ASGI server still buffers the request body first, so a hard byte limit
# belongs in front of the app.
MAX_CSV_TEXT_CHARS = 2 * 1024 * 1024
=======
# Upper bound on a single CSV import body, in characters.
#
# `parse_csv_import` is CPU-bound and runs synchronously on the app's single
# event loop, so an unbounded `csv_text` makes the worst-case stall unbounded
# too: one oversized import blocks every other request in the process. The
# limit is enforced by pydantic, which means it rejects with a 422 before the
# parser ever sees the body.
#
# 4 MiB of characters is roughly 25k-40k typical CTE rows -- far above any
# real demo or design-partner import, and far below a body that would stall
# the loop noticeably.
MAX_CSV_IMPORT_CHARS = 4 * 1024 * 1024
>>>>>>> origin/main


class CSVImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_type: CSVImportType
<<<<<<< HEAD
    csv_text: str = Field(max_length=MAX_CSV_TEXT_CHARS)
=======
    csv_text: str = Field(max_length=MAX_CSV_IMPORT_CHARS)
>>>>>>> origin/main
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

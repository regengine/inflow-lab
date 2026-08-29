from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..cte_rules import WarningSeverity
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
    # Declared explicitly rather than left to be refused as an extra. The
    # contract reference states RegEngine "resolves tenant from body, then
    # API-key lookup, then RBAC principal", so a body-level tenant id is part
    # of the shape real RegEngine accepts -- and this route exists to mirror
    # live validation for external callers. Rejecting it made the mock
    # stricter than the thing it simulates, which is a parity break in the
    # direction that produces false confidence.
    # exclude=True is serialization-only in pydantic: the field is still
    # populated from an incoming body, but never appears in model_dump(). That
    # matters because this same model builds the OUTBOUND payload in
    # LiveRegEngineClient.ingest -- adding a field that serialized would have
    # silently changed the bytes sent to real RegEngine (and the bytes the
    # HMAC is computed over). Tenant is carried to live RegEngine in the
    # X-Tenant-ID header, which is unchanged.
    tenant_id: str | None = Field(default=None, exclude=True)
    events: list[RegEngineEvent]

    @field_validator("events", mode="before")
    @classmethod
    def _reject_unknown_event_fields(cls, value: Any) -> Any:
        """Forbid unknown keys on each EVENT, not just the envelope.

        The envelope's extra="forbid" catches a misspelled "events" key. It
        does nothing about the far likelier integration typo -- a misspelled
        field inside an event ("product_desc", "traceability_lot_codes") --
        because RegEngineEvent itself is permissive, so those were silently
        dropped and the event ingested as if it were complete. Surfacing
        exactly that is what this simulator exists for.

        Enforced here rather than by putting extra="forbid" on RegEngineEvent,
        because that model is also what StoredEventRecord persists and reloads.
        Forbidding there would make an event written by a version carrying one
        extra field unreadable on the way back in -- silently skipped, since
        the read path logs and continues -- turning a typo guard into data
        loss.

        Only raw dicts are checked, so internal construction from already-built
        RegEngineEvent instances is untouched.
        """
        if not isinstance(value, list):
            return value
        known_fields = set(RegEngineEvent.model_fields)
        for index, event in enumerate(value):
            if not isinstance(event, dict):
                continue
            unknown = sorted(set(event) - known_fields)
            if unknown:
                raise ValueError(
                    f"events[{index}] has unknown field(s) {unknown}. Allowed fields: "
                    f"{sorted(known_fields)}. Per-event data that is not a defined KDE "
                    "field belongs inside 'kdes'."
                )
        return value


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


# Enforced ceiling on one CSV import body (#136). The parse behind this
# field is CPU-bound; app/controller.py's import_csv offloads it to a worker
# thread so it no longer blocks the event loop, but an unbounded body still
# makes the worst case unbounded -- one request can pin a pool thread and
# hold several copies of the text in memory (raw body, decoded str, parsed
# rows, built events) for as long as it takes.
#
# 2,000,000 characters is roughly 15k rows of the shipped seed_lots/
# scheduled_events shapes (~130 characters each) -- far past anything this
# simulator is for, and past the demo fixtures and README examples by orders
# of magnitude, while still bounding a single request. Counted in characters
# rather than encoded bytes deliberately: a str of N characters costs at most
# 4N bytes however it is measured, so the character count is the same bound
# without paying for an encode() of the whole body just to check it.
#
# NOTE this caps the *field*, which is where #136 asks for it. It is not a
# request-body cap: the ASGI server has already read and decoded the whole
# body by the time this runs, so an oversized upload is still read once
# before being refused. A body-size limit belongs in middleware and is a
# separate change.
MAX_CSV_TEXT_CHARS = 2_000_000


class CSVImportRequest(BaseModel):
    # extra="forbid" (#143): request body for POST /api/import/csv.
    model_config = ConfigDict(extra="forbid")

    import_type: CSVImportType
    csv_text: str
    source: str | None = None
    delivery: DeliveryConfig | None = None

    @field_validator("csv_text")
    @classmethod
    def validate_csv_text_size(cls, value: str) -> str:
        """Refuse an oversized import body outright (#136).

        A ``field_validator`` rather than ``Field(max_length=...)`` to match
        this module's existing convention (see
        ``DeliveryRetryRequest.validate_limit``) and, more usefully, so the
        422 tells an operator what actually happened and what to do about
        it -- the generic constraint message names neither the size they
        sent nor the remedy.
        """
        if len(value) > MAX_CSV_TEXT_CHARS:
            raise ValueError(
                f"csv_text is {len(value)} characters, above the "
                f"{MAX_CSV_TEXT_CHARS}-character import limit. Split the file "
                "and import it in several requests."
            )
        return value


class CSVImportError(BaseModel):
    row: int
    field: str | None = None
    message: str


class CSVImportWarning(BaseModel):
    row: int
    field: str | None = None
    message: str
    # Carried through from CTEValidationWarning (#189). Without it the
    # import panel could only tell a required-tier gap from an advisory one
    # by string-matching the message, which no consumer did -- so an FDA-
    # mandatory KDE and a nice-to-have read identically in the response and
    # in the console. Defaulted so a warning raised by the importer itself,
    # about the file rather than the event's KDEs, needs no new argument.
    severity: WarningSeverity = "recommended"


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

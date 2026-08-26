from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, NamedTuple
from uuid import uuid4

from .cte_rules import REQUIRED_KDES
from .schemas.domain import DestinationMode, RegEngineEvent
from .schemas.ingestion import IngestPayload, IngestResponseEvent, MockIngestResponse
from .store import EventStore


# Mirrors RegEngine's WebhookPayload constraint: events accepts 1-500 items.
MIN_BATCH_EVENTS = 1
MAX_BATCH_EVENTS = 500
# Mirrors RegEngine's Pydantic timestamp validator: >24h in the future is rejected.
MAX_FUTURE_HOURS = 24
# Mirrors RegEngine's idempotency-replay window: a repeated Idempotency-Key
# within this many hours replays the stored response; once an entry ages
# out it is treated as unseen rather than replayed (#120).
IDEMPOTENCY_TTL_HOURS = 24
# Mirrors RegEngine's model-level location validator: at least one of these
# must be present (top-level or KDE) for the event to be accepted.
LOCATION_KDE_FIELDS = (
    "ship_from_location",
    "ship_to_location",
    "receiving_location",
    "ship_from_gln",
    "ship_to_gln",
)
_IDEMPOTENCY_CACHE_LIMIT = 1024

# Friction injection codes -> the HTTP failure a real customer hits. Keyed by
# the DeliveryConfig.mock_friction values so operators can rehearse each
# failure mode without touching a live RegEngine.
FRICTION_RESPONSES: dict[str, tuple[int, str]] = {
    "invalid_key": (401, "Invalid API key"),
    "subscription_inactive": (
        402,
        "Subscription inactive for tenant. Activate billing (status must be "
        "active or trialing) to resume ingestion.",
    ),
    "rate_limit": (
        429,
        "Rate limit exceeded for webhooks.ingest. Retry after 60 seconds.",
    ),
}


class MockRegEngineHTTPError(Exception):
    """A request-level failure, mirroring a non-2xx RegEngine response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class _CachedIdempotencyEntry(NamedTuple):
    """One idempotency-cache slot: the response to replay and when it landed.

    ``inserted_at`` is stamped from the service's injectable clock rather
    than a bare ``datetime.now(UTC)`` call, so a test can advance time
    deterministically to exercise the 24h boundary instead of sleeping
    through it (#120).
    """

    response: MockIngestResponse
    inserted_at: datetime


def _resume_chain_hash(store: EventStore) -> str:
    """Reconstruct the mock's chain_hash lineage from the store's persisted log.

    Mirrors EventStore._load_from_disk's own resume-from-disk pattern (see
    app/store.py) so a process restart continues the same hash chain
    instead of forking a fresh one from "" (#122). Records are walked
    newest-first by sequence_no; the first one that is both mock-delivered
    and accepted wins its chain_hash -- live-delivered records carry
    RegEngine's own unrelated chain, and rejected records carry none, so
    neither can stand in for the mock's own lineage tip.
    """
    records = sorted(store.read_persisted_records(), key=lambda record: record.sequence_no)
    for record in reversed(records):
        if record.destination_mode != DestinationMode.MOCK:
            continue
        chain_hash = (record.delivery_response or {}).get("chain_hash")
        if isinstance(chain_hash, str) and chain_hash:
            return chain_hash
    return ""


class MockRegEngineService:
    """In-process stand-in for RegEngine's POST /api/v1/webhooks/ingest.

    Unlike the original always-accept mock, this mirrors the live webhook's
    validation so the demo experience matches what a customer hits in the
    wild: batch-size bounds, strict per-CTE KDE checks (exact key lookup,
    no aliasing), the location-identifier requirement, in-batch duplicate
    rejection, future-timestamp rejection, and 24h idempotency replays.

    ``chain_hash`` and the idempotency cache otherwise live only in process
    memory. Construct with ``store=`` to seed ``chain_hash`` from a
    persisted event log so a restart resumes the same lineage instead of
    forking a new one from "" (#122). The idempotency cache is
    deliberately NOT reloaded from disk: it is a best-effort, per-process
    replay guard, and losing a few hours of it on restart just means one
    retry re-ingests as new -- far cheaper than persisting every
    idempotency key indefinitely.
    """

    def __init__(
        self,
        store: EventStore | None = None,
        *,
        idempotency_ttl: timedelta = timedelta(hours=IDEMPOTENCY_TTL_HOURS),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._chain_hash = _resume_chain_hash(store) if store is not None else ""
        self._idempotency_cache: OrderedDict[str, _CachedIdempotencyEntry] = OrderedDict()
        # Plain instance attributes (not module constants captured at import
        # time) so a test can inject a short TTL and/or a controllable clock
        # to cross the replay boundary deterministically, without sleeping
        # (#120). Production callers get the real 24h window and wall clock.
        self.idempotency_ttl = idempotency_ttl
        self._clock = clock

    def reset(self) -> None:
        self._chain_hash = ""
        self._idempotency_cache.clear()

    def ingest(
        self,
        payload: IngestPayload,
        idempotency_key: str | None = None,
        friction: tuple[str, ...] = (),
    ) -> MockIngestResponse:
        for code in friction:
            failure = FRICTION_RESPONSES.get(code)
            if failure is not None:
                raise MockRegEngineHTTPError(*failure)

        if len(payload.events) < MIN_BATCH_EVENTS:
            raise MockRegEngineHTTPError(
                422,
                f"events accepts at least {MIN_BATCH_EVENTS} item per batch",
            )
        if len(payload.events) > MAX_BATCH_EVENTS:
            raise MockRegEngineHTTPError(
                422,
                f"events accepts at most {MAX_BATCH_EVENTS} items per batch",
            )

        now = self._clock()
        if idempotency_key and idempotency_key in self._idempotency_cache:
            cached = self._idempotency_cache[idempotency_key]
            if now - cached.inserted_at < self.idempotency_ttl:
                self._idempotency_cache.move_to_end(idempotency_key)
                return cached.response
            # Past the replay window: RegEngine treats the key as unseen
            # rather than replaying a stale result, so drop it and fall
            # through to ingest normally -- a fresh entry is cached below.
            del self._idempotency_cache[idempotency_key]

        response_events: list[IngestResponseEvent] = []
        accepted = 0
        rejected = 0
        seen_batch_keys: set[str] = set()
        for event in payload.events:
            errors = validate_event_like_regengine(event)
            batch_key = "|".join(
                (
                    event.cte_type.value,
                    event.traceability_lot_code,
                    event.timestamp.isoformat(),
                    event.location_name or event.location_gln or "",
                )
            )
            if batch_key in seen_batch_keys:
                errors.append("Duplicate event in batch (same CTE, lot, timestamp, and location)")
            seen_batch_keys.add(batch_key)

            if errors:
                rejected += 1
                response_events.append(
                    IngestResponseEvent(
                        traceability_lot_code=event.traceability_lot_code,
                        cte_type=event.cte_type,
                        status="rejected",
                        errors=errors,
                    )
                )
                continue

            raw = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            sha256_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            chain_seed = f"{self._chain_hash}:{sha256_hash}".encode("utf-8")
            self._chain_hash = hashlib.sha256(chain_seed).hexdigest()
            accepted += 1
            response_events.append(
                IngestResponseEvent(
                    traceability_lot_code=event.traceability_lot_code,
                    cte_type=event.cte_type,
                    status="accepted",
                    event_id=str(uuid4()),
                    sha256_hash=sha256_hash,
                    chain_hash=self._chain_hash,
                )
            )

        response = MockIngestResponse(
            accepted=accepted,
            rejected=rejected,
            total=accepted + rejected,
            events=response_events,
            ingestion_timestamp=now,
        )
        if idempotency_key:
            self._idempotency_cache[idempotency_key] = _CachedIdempotencyEntry(
                response=response, inserted_at=now
            )
            while len(self._idempotency_cache) > _IDEMPOTENCY_CACHE_LIMIT:
                self._idempotency_cache.popitem(last=False)
        return response


def validate_event_like_regengine(event: RegEngineEvent) -> list[str]:
    """Per-event checks mirroring RegEngine's webhook validation.

    Uses the same merged namespace RegEngine builds (top-level fields plus
    the kdes dict) with strict string lookup — deliberately NOT the lenient
    aliasing in cte_rules.merged_event_values, because the live validator
    does not alias (e.g. reference_document_type never satisfies
    reference_document).
    """
    errors: list[str] = []

    if len(event.traceability_lot_code) < 3:
        errors.append("traceability_lot_code must be at least 3 characters")
    if not 1 <= len(event.product_description) <= 500:
        errors.append("product_description must be 1-500 characters")
    if event.quantity <= 0:
        errors.append("quantity must be greater than 0")

    timestamp = event.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if timestamp > datetime.now(UTC) + timedelta(hours=MAX_FUTURE_HOURS):
        errors.append(f"timestamp is more than {MAX_FUTURE_HOURS} hours in the future")

    available = _strict_merged_values(event)
    has_location = any(
        _has_value(available.get(field))
        for field in ("location_name", "location_gln", *LOCATION_KDE_FIELDS)
    )
    if not has_location:
        errors.append(
            "At least one location identifier is required "
            "(location_gln, location_name, or a location KDE)"
        )

    required = REQUIRED_KDES.get(event.cte_type, ())
    missing = [field for field in required if not _has_value(available.get(field))]
    if missing:
        errors.append(
            f"Missing required KDEs for {event.cte_type.value}: {', '.join(missing)}"
        )
    return errors


def _strict_merged_values(event: RegEngineEvent) -> dict[str, Any]:
    return {
        "traceability_lot_code": event.traceability_lot_code,
        "product_description": event.product_description,
        "quantity": event.quantity,
        "unit_of_measure": event.unit_of_measure,
        "location_name": event.location_name,
        "location_gln": event.location_gln,
        **event.kdes,
    }


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Protocol
from uuid import uuid4

from .cte_rules import REQUIRED_KDES
from .regengine_client import WEBHOOK_HMAC_SECRET_ENV
from .schemas.domain import RegEngineEvent, StoredEventRecord
from .schemas.ingestion import IngestPayload, IngestResponseEvent, MockIngestResponse


# Mirrors RegEngine's WebhookPayload constraint: events accepts 1-500 items.
MIN_BATCH_EVENTS = 1
MAX_BATCH_EVENTS = 500
# Mirrors RegEngine's Pydantic timestamp validator: >24h in the future is rejected.
MAX_FUTURE_HOURS = 24
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
# Mirrors RegEngine's documented replay window: a cached response for a given
# Idempotency-Key is replayed for 24 hours and is a cache miss after that.
IDEMPOTENCY_TTL = timedelta(hours=24)

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


class PersistedEventSource(Protocol):
    """The slice of ``EventStore`` the mock needs to resume its chain hash."""

    def read_persisted_records(self, persist_path: str | None = ...) -> list[StoredEventRecord]:
        ...


@dataclass(slots=True)
class _CachedResponse:
    response: MockIngestResponse
    stored_at: datetime


class MockRegEngineService:
    """In-process stand-in for RegEngine's POST /api/v1/webhooks/ingest.

    Unlike the original always-accept mock, this mirrors the live webhook's
    validation so the demo experience matches what a customer hits in the
    wild: strict per-CTE KDE checks (exact key lookup, no aliasing), the
    location-identifier requirement, batch caps (1-500 events), in-batch
    duplicate rejection, future-timestamp rejection, optional HMAC signature
    verification, and 24h idempotency replays.

    The chain hash is resumed from the persisted event log when an event
    source is attached (see :meth:`attach_event_source`), so a restart of a
    volume-backed deployment continues the existing chain instead of forking
    a new one from "".

    The idempotency cache is per-process and best-effort: it is bounded both
    by ``IDEMPOTENCY_TTL`` (24h, matching the documented replay window) and
    by ``_IDEMPOTENCY_CACHE_LIMIT`` entries, and it is not persisted.
    """

    def __init__(self, event_source: PersistedEventSource | None = None) -> None:
        self._chain_hash = ""
        self._idempotency_cache: OrderedDict[str, _CachedResponse] = OrderedDict()
        self._event_source: PersistedEventSource | None = None
        self._chain_resumed = True
        self.time_source: Callable[[], datetime] = lambda: datetime.now(UTC)
        if event_source is not None:
            self.attach_event_source(event_source)

    @property
    def chain_hash(self) -> str:
        """The current head of the mock's hash chain ("" before any event)."""
        return self._chain_hash

    def attach_event_source(self, event_source: PersistedEventSource | None) -> None:
        """Point the mock at the persisted event log it should resume from.

        Resumption is lazy: the chain hash is rebuilt from disk on the next
        ingest, so attaching costs nothing at import/startup time.
        """
        self._event_source = event_source
        self._chain_resumed = event_source is None

    def resume_chain_from_records(self, records: Iterable[StoredEventRecord]) -> str:
        """Seed the chain hash from already-loaded persisted records."""
        self._chain_hash = chain_hash_from_records(records)
        self._chain_resumed = True
        return self._chain_hash

    def _ensure_chain_resumed(self) -> None:
        if self._chain_resumed:
            return
        source = self._event_source
        # Mark resumed first: a source that cannot be read (missing file,
        # unreadable line) must not retry on every single ingest.
        self._chain_resumed = True
        if source is None:
            return
        try:
            records = source.read_persisted_records()
        except (OSError, ValueError):
            return
        self._chain_hash = chain_hash_from_records(records)

    def reset(self) -> None:
        self._chain_hash = ""
        self._idempotency_cache.clear()
        # A reset clears the persisted log too, so there is nothing to resume.
        self._chain_resumed = True

    def verify_signature(self, body_bytes: bytes, signature: str | None) -> None:
        """Mirror RegEngine's ``_verify_webhook_signature``.

        No-ops when ``REGENGINE_WEBHOOK_HMAC_SECRET`` is unset (the default,
        and the pre-signing migration ramp on both sides). When the secret is
        configured, a missing or non-matching ``X-Webhook-Signature`` raises a
        401 exactly as the live webhook does, so body-bytes drift in the
        client's signer is caught locally instead of in production.
        """
        expected = expected_signature(body_bytes)
        if expected is None:
            return
        if not signature:
            raise MockRegEngineHTTPError(401, "Missing X-Webhook-Signature header")
        candidate = signature.strip()
        if candidate.startswith("sha256="):
            candidate = candidate[len("sha256=") :]
        if not hmac.compare_digest(candidate, expected.removeprefix("sha256=")):
            raise MockRegEngineHTTPError(401, "Invalid webhook signature")

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

        if not payload.events:
            raise MockRegEngineHTTPError(
                422,
                f"events accepts {MIN_BATCH_EVENTS}-{MAX_BATCH_EVENTS} items per batch",
            )
        if len(payload.events) > MAX_BATCH_EVENTS:
            raise MockRegEngineHTTPError(
                422,
                f"events accepts at most {MAX_BATCH_EVENTS} items per batch",
            )

        now = self.time_source()
        self._expire_idempotency_entries(now)
        cached = self._idempotency_cache.get(idempotency_key) if idempotency_key else None
        if cached is not None:
            self._idempotency_cache.move_to_end(idempotency_key)
            return cached.response

        self._ensure_chain_resumed()

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
            ingestion_timestamp=datetime.now(UTC),
        )
        if idempotency_key:
            self._idempotency_cache[idempotency_key] = _CachedResponse(
                response=response,
                stored_at=now,
            )
            self._idempotency_cache.move_to_end(idempotency_key)
            while len(self._idempotency_cache) > _IDEMPOTENCY_CACHE_LIMIT:
                self._idempotency_cache.popitem(last=False)
        return response

    def _expire_idempotency_entries(self, now: datetime) -> None:
        """Drop entries older than the documented 24h replay window.

        Entries are inserted in time order, so this stops at the first live
        entry instead of scanning the whole cache.
        """
        cutoff = now - IDEMPOTENCY_TTL
        while self._idempotency_cache:
            key, entry = next(iter(self._idempotency_cache.items()))
            if entry.stored_at > cutoff:
                return
            del self._idempotency_cache[key]


def chain_hash_from_records(records: Iterable[StoredEventRecord]) -> str:
    """Return the newest persisted ``chain_hash``, or "" when there is none.

    ``EventStore.read_persisted_records`` returns records oldest-first, but
    replayed/retried records can be appended out of order, so the head of the
    chain is the record with the highest ``sequence_no`` that carries an
    accepted delivery response. File order breaks ties.
    """
    head = ""
    head_key: tuple[int, int] | None = None
    for index, record in enumerate(records):
        response = record.delivery_response
        if not isinstance(response, dict):
            continue
        chain_hash = response.get("chain_hash")
        if not isinstance(chain_hash, str) or not chain_hash:
            continue
        key = (record.sequence_no, index)
        if head_key is None or key > head_key:
            head_key = key
            head = chain_hash
    return head


def expected_signature(body_bytes: bytes) -> str | None:
    """The ``X-Webhook-Signature`` value these bytes must carry, or None.

    Returns None when ``REGENGINE_WEBHOOK_HMAC_SECRET`` is unset, matching
    ``regengine_client._build_signature_header`` so the mock and the live
    client agree on both the secret source and the digest format.
    """
    secret = os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip()
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


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

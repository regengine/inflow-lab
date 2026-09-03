from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Protocol
from uuid import uuid4

from pydantic import ValidationError

from .cte_rules import REQUIRED_KDES
<<<<<<< HEAD
from .schemas.domain import RegEngineEvent
from .schemas.ingestion import (
    MAX_BATCH_EVENTS,
    IngestPayload,
    IngestResponseEvent,
    MockIngestResponse,
)
=======
from .regengine_client import WEBHOOK_HMAC_SECRET_ENV
from .schemas.domain import RegEngineEvent, StoredEventRecord
from .schemas.ingestion import IngestPayload, IngestResponseEvent, MockIngestResponse

>>>>>>> origin/main

logger = logging.getLogger(__name__)

# Mirrors RegEngine's WebhookPayload constraint: events accepts 1-500 items.
<<<<<<< HEAD
# Defined on the schema so it is enforced before the list is materialised;
# re-exported here because the service still checks it for direct callers.
__all__ = ["MAX_BATCH_EVENTS", "FRICTION_RESPONSES", "MockRegEngineHTTPError", "MockRegEngineService"]
=======
MIN_BATCH_EVENTS = 1
MAX_BATCH_EVENTS = 500
# Hard ceiling on a single ingest request body, in bytes.
#
# The batch cap above can only be enforced after the body has been parsed into
# events, and parsing is the expensive part. Signature verification now runs
# before parsing (see the mock router), so an unsigned caller buys nothing but
# an HMAC over the raw bytes — but a *well-formed* unsigned batch would still
# have its bytes read into memory first. This bounds that: 500 events at ~8 KiB
# each is ~4 MiB, so the ceiling only bites on a body no legitimate batch can
# reach, and it is checked against Content-Length before the body is read.
MAX_INGEST_BODY_BYTES = 4 * 1024 * 1024
# A sha256 hex digest and nothing else. `hmac.compare_digest` raises TypeError
# on str operands carrying any character outside ASCII, and Starlette decodes
# headers as latin-1, so a signature header with any byte >= 0x80 used to reach
# compare_digest and escape as an unauthenticated 500 (with a traceback).
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]+", re.IGNORECASE)
>>>>>>> origin/main
# Mirrors RegEngine's Pydantic timestamp validator: >24h in the future is rejected.
MAX_FUTURE_HOURS = 24
# Mirrors RegEngine's handler-level replay guard
# (webhook_router_v2/security.py::_validate_event_timestamp_window):
# WEBHOOK_MAX_EVENT_AGE_DAYS, default "90". No RegEngine deployment config
# overrides it, so 90 days is what a live tenant actually enforces.
MAX_EVENT_AGE_DAYS = 90
MAX_EVENT_AGE_DAYS_ENV = "REGENGINE_MOCK_MAX_EVENT_AGE_DAYS"
EVENT_AGE_MODE_ENV = "REGENGINE_MOCK_EVENT_AGE_MODE"
# Enforced by default: the demo fixtures used to carry fixed 2026-02
# timestamps, so rejecting on age would have taken the dashboard demo and the
# browser smoke down with it, and the default had to be "warn". The fixtures
# are now rebased onto the live replay window (see app/demo_fixtures.py), so
# the mock enforces the floor like a live tenant does. "warn" still accepts
# but logs every out-of-window event at WARNING and counts it on the response
# headers; "off" drops the check entirely. Both remain available for replaying
# genuinely historical data through the mock.
EVENT_AGE_MODE_WARN = "warn"
EVENT_AGE_MODE_REJECT = "reject"
EVENT_AGE_MODE_OFF = "off"
EVENT_AGE_MODES = (EVENT_AGE_MODE_WARN, EVENT_AGE_MODE_REJECT, EVENT_AGE_MODE_OFF)
DEFAULT_EVENT_AGE_MODE = EVENT_AGE_MODE_REJECT

# Where RegEngine draws the line between Pydantic request-body validation and
# handler logic. Anything in BATCH_FATAL_FIELD_CHECKS is a field constraint on
# RegEngine's ``IngestEvent``: FastAPI 422s the WHOLE request before the route
# handler runs, so no event in the batch is stored. Anything in
# PER_EVENT_HANDLER_CHECKS runs inside the handler and rejects only the
# offending event inside an HTTP 200 response. Pinned by
# tests/test_mock_rejection_parity.py so a new RegEngine constraint that lands
# on the wrong side of this line is caught here rather than in production.
BATCH_FATAL_FIELD_CHECKS = (
    "traceability_lot_code",
    "product_description",
    "quantity",
    "timestamp",
)
PER_EVENT_HANDLER_CHECKS = (
    "location",
    "required_kdes",
    "event_age",
    "duplicate_in_batch",
)
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
    duplicate rejection, future-timestamp rejection, the 90-day replay
    window, optional HMAC signature verification, and 24h idempotency
    replays.

    It also mirrors *where* each check bites. RegEngine enforces the four
    ``IngestEvent`` field constraints (lot-code length, description length,
    positive quantity, 24h future ceiling) as Pydantic request-body
    validation, so FastAPI 422s the whole request and the batch is lost;
    those raise :class:`MockRegEngineHTTPError` here rather than appearing as
    a per-event rejection in a 200. Location, required-KDE, replay-window and
    in-batch-duplicate checks run in RegEngine's handler and reject a single
    event inside a 200. See ``BATCH_FATAL_FIELD_CHECKS`` /
    ``PER_EVENT_HANDLER_CHECKS`` and #101.

    The replay-window floor is configurable via
    ``REGENGINE_MOCK_EVENT_AGE_MODE`` (warn/reject/off, default reject — the
    shipped fixtures now sit inside the window) and
    ``REGENGINE_MOCK_MAX_EVENT_AGE_DAYS`` (default 90); see the constant
    comments and #102.

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
        # Out-of-window events accepted by the most recent ingest because the
        # age mode is "warn". Read by the mock router to surface them on the
        # response headers; empty in "reject"/"off" mode.
        self.last_age_window_warnings: tuple[str, ...] = ()
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
        self.last_age_window_warnings = ()
        # A reset clears the persisted log too, so there is nothing to resume.
        self._chain_resumed = True

    def verify_signature(self, body_bytes: bytes, signature: str | None) -> None:
        """Mirror RegEngine's ``_verify_webhook_signature``.

        No-ops when ``REGENGINE_WEBHOOK_HMAC_SECRET`` is unset (the default,
        and the pre-signing migration ramp on both sides). When the secret is
        configured, a missing or non-matching ``X-Webhook-Signature`` raises a
        401 exactly as the live webhook does, so body-bytes drift in the
        client's signer is caught locally instead of in production.

        A header that is not a sha256 hex digest of the expected length is
        rejected as an ordinary 401 *before* the comparison. That pre-check is
        not a timing side channel worth worrying about — it leaks only the
        digest algorithm and length, which the contract publishes — and it is
        what keeps a non-ASCII header value out of ``hmac.compare_digest``,
        whose ``TypeError`` otherwise escapes as an unauthenticated 500. The
        comparison that actually matters stays constant-time.
        """
        expected = expected_signature(body_bytes)
        if expected is None:
            return
        if not signature:
            raise MockRegEngineHTTPError(401, "Missing X-Webhook-Signature header")
        candidate = signature.strip()
        if candidate.startswith("sha256="):
            candidate = candidate[len("sha256=") :]
        digest = expected.removeprefix("sha256=")
        # Same 401 wording as a digest mismatch: a malformed header must not be
        # distinguishable from a wrong one.
        if len(candidate) != len(digest) or not _HEX_DIGEST_RE.fullmatch(candidate):
            raise MockRegEngineHTTPError(401, "Invalid webhook signature")
        if not hmac.compare_digest(candidate, digest):
            raise MockRegEngineHTTPError(401, "Invalid webhook signature")

    def ingest(
        self,
        payload: IngestPayload,
        idempotency_key: str | None = None,
        friction: tuple[str, ...] = (),
    ) -> MockIngestResponse:
<<<<<<< HEAD
        # An unrecognised code used to be skipped in silence and the batch
        # accepted with a 200 -- an operator rehearsing a failure mode that
        # never fires, which is the exact class of silent no-op this simulator
        # exists to surface. `MockFrictionCode` already pins the HTTP and
        # config paths to the known set; this guards a direct caller.
        # Validated up front so which code is reported does not depend on the
        # order they were listed in.
        # Materialised once: the previous `for code in friction` accepted any
        # iterable, and indexing `friction[0]` instead broke sets, dict views
        # and generators -- an empty generator went from a clean no-op to a
        # TypeError. Taking a tuple keeps the old tolerance and makes the
        # membership scan safe against a one-shot iterator.
        codes = tuple(friction)
        unknown = sorted({code for code in codes if code not in FRICTION_RESPONSES})
        if unknown:
            raise MockRegEngineHTTPError(
                422,
                f"Unknown mock friction code(s): {', '.join(unknown)}. "
                f"Known codes: {', '.join(sorted(FRICTION_RESPONSES))}.",
            )
        if codes:
            raise MockRegEngineHTTPError(*FRICTION_RESPONSES[codes[0]])
=======
        # Per-ingest, so a 422 or an idempotency replay never reports the
        # previous batch's out-of-window events.
        self.last_age_window_warnings = ()

        for code in friction:
            failure = FRICTION_RESPONSES.get(code)
            if failure is not None:
                raise MockRegEngineHTTPError(*failure)
>>>>>>> origin/main

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
        # Field constraints are request-body validation on RegEngine's side:
        # FastAPI 422s before the handler (and before any idempotency
        # bookkeeping) runs, so ONE bad event loses the whole batch. Scanning
        # every event first means the 422 names them all, the way a real
        # FastAPI validation error does.
        fatal = [
            f"events.{index}.{field}: {message}"
            for index, event in enumerate(payload.events)
            for field, message in field_constraint_errors(event, now=now)
        ]
        if fatal:
            raise MockRegEngineHTTPError(422, _batch_fatal_detail(fatal, len(payload.events)))

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
        age_mode = event_age_mode()
        age_warnings: list[str] = []
        for event in payload.events:
            errors = handler_rejection_errors(event, now=now)
            if age_mode != EVENT_AGE_MODE_REJECT:
                # Demote (warn) or drop (off) the replay-window verdict, but
                # never silently: a warn-mode violation is logged and counted
                # so the demo cannot look clean while live would reject.
                out_of_window = replay_window_errors(event, now=now)
                errors = [error for error in errors if error not in out_of_window]
                if out_of_window and age_mode == EVENT_AGE_MODE_WARN:
                    warning = (
                        f"{event.traceability_lot_code} ({event.cte_type.value}): "
                        f"{out_of_window[0]}"
                    )
                    age_warnings.append(warning)
                    logger.warning(
                        "Mock accepted an event live RegEngine would reject "
                        "(REGENGINE_MOCK_EVENT_AGE_MODE=%s): %s",
                        age_mode,
                        warning,
                    )
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
            chain_seed = f"{self._chain_hash}:{sha256_hash}".encode()
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

        self.last_age_window_warnings = tuple(age_warnings)
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


def _batch_fatal_detail(failures: list[str], total_events: int) -> str:
    """The 422 body RegEngine's FastAPI layer produces for a bad request body.

    Spelling out that nothing was stored matters: the whole point of #101 is
    that an operator must not read a field-constraint failure as "one row
    bounced".
    """
    return (
        "Request body failed validation before ingest: "
        f"{len(failures)} field constraint violation(s). The entire batch of "
        f"{total_events} event(s) was rejected and nothing was stored. "
        + "; ".join(failures)
    )


def parse_ingest_payload(raw_body: bytes) -> IngestPayload:
    """Parse a raw ingest body, 422ing in the mock's own voice on failure.

    This exists so the HTTP route can verify the HMAC signature *before* the
    body is parsed. Live RegEngine verifies the signature ahead of body
    validation, so an unsigned or badly-signed caller there sees a 401 and
    nothing else; leaving the parse to FastAPI's request-body binding made the
    mock answer such a caller with a field-by-field 422 that enumerated the
    accepted CTE vocabulary — a schema oracle live never opens.

    The 422 detail is a plain string, matching :func:`_batch_fatal_detail`
    rather than FastAPI's list of error dicts: the mock already reports
    RegEngine's request-fatal field constraints that way (see #101), and one
    shape for "the whole batch died before ingest" is what a client can code
    against.
    """
    try:
        return IngestPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        raise MockRegEngineHTTPError(422, _request_body_detail(exc)) from exc


def _request_body_detail(exc: ValidationError) -> str:
    failures = [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    ]
    return (
        "Request body failed validation before ingest: "
        f"{len(failures)} field constraint violation(s). The entire batch was "
        "rejected and nothing was stored. " + "; ".join(failures)
    )


def validate_friction_codes(codes: Iterable[str]) -> tuple[str, ...]:
    """Reject friction codes this mock does not implement.

    :meth:`MockRegEngineService.ingest` only acts on codes present in
    ``FRICTION_RESPONSES``, so an unrecognised code used to sail through as a
    clean 200. The in-process path never hit that — ``DeliveryConfig``'s
    ``mock_friction`` is a typed ``Literal`` and pydantic rejects a typo — but
    the ``X-Mock-Friction`` header path is exactly the one a customer testing
    their own client uses, and an operator rehearsing a 402 against a
    misspelled code would read the green 200 as "that failure mode is
    handled". Surfacing silent no-ops is the entire point of this simulator,
    so an unknown code is a 400 that names it.
    """
    requested = tuple(codes)
    unknown = [code for code in requested if code not in FRICTION_RESPONSES]
    if unknown:
        raise MockRegEngineHTTPError(
            400,
            f"Unknown X-Mock-Friction code(s): {', '.join(unknown)}. "
            f"Accepted codes: {', '.join(sorted(FRICTION_RESPONSES))}.",
        )
    return requested


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


def max_event_age_days() -> int:
    """The configured replay-window floor in days (RegEngine default: 90)."""
    raw = os.getenv(MAX_EVENT_AGE_DAYS_ENV, "").strip()
    if not raw:
        return MAX_EVENT_AGE_DAYS
    try:
        days = int(raw)
    except ValueError:
        return MAX_EVENT_AGE_DAYS
    return days if days > 0 else MAX_EVENT_AGE_DAYS


def event_age_mode() -> str:
    """How the mock treats out-of-window events: reject (default), warn, off."""
    mode = os.getenv(EVENT_AGE_MODE_ENV, "").strip().lower()
    return mode if mode in EVENT_AGE_MODES else DEFAULT_EVENT_AGE_MODE


def _aware(timestamp: datetime) -> datetime:
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


def field_constraint_errors(
    event: RegEngineEvent, now: datetime | None = None
) -> list[tuple[str, str]]:
    """The checks RegEngine enforces as ``IngestEvent`` Pydantic constraints.

    Returns ``(field, message)`` pairs. Every one of these is a field-level
    constraint (``min_length``/``max_length``/``gt`` or a
    ``@field_validator``) on RegEngine's request body, so FastAPI 422s the
    ENTIRE request before the ingest handler runs — no event in the batch is
    stored. Reporting any of these as a per-event rejection inside an HTTP
    200 is the "green demo, failing live post" drift #101 describes.
    """
    moment = now or datetime.now(UTC)
    errors: list[tuple[str, str]] = []
    if len(event.traceability_lot_code) < 3:
        errors.append(
            ("traceability_lot_code", "traceability_lot_code must be at least 3 characters")
        )
    if not 1 <= len(event.product_description) <= 500:
        errors.append(("product_description", "product_description must be 1-500 characters"))
    if event.quantity <= 0:
        errors.append(("quantity", "quantity must be greater than 0"))
    if _aware(event.timestamp) > moment + timedelta(hours=MAX_FUTURE_HOURS):
        errors.append(
            ("timestamp", f"timestamp is more than {MAX_FUTURE_HOURS} hours in the future")
        )
    return errors


def replay_window_errors(event: RegEngineEvent, now: datetime | None = None) -> list[str]:
    """The age-floor check RegEngine runs per event inside the handler.

    Mirrors ``_validate_event_timestamp_window``: anything older than
    ``WEBHOOK_MAX_EVENT_AGE_DAYS`` (default 90) is rejected with "replay
    window exceeded" — a per-event rejection inside an HTTP 200, not a
    request-fatal 422. See #102.
    """
    moment = now or datetime.now(UTC)
    days = max_event_age_days()
    if _aware(event.timestamp) < moment - timedelta(days=days):
        return [
            f"Event timestamp {_aware(event.timestamp).isoformat()} is older than "
            f"WEBHOOK_MAX_EVENT_AGE_DAYS={days} — replay window exceeded"
        ]
    return []


def handler_rejection_errors(event: RegEngineEvent, now: datetime | None = None) -> list[str]:
    """The checks RegEngine runs inside the handler, rejecting one event only.

    Uses the same merged namespace RegEngine builds (top-level fields plus
    the kdes dict) with strict string lookup — deliberately NOT the lenient
    aliasing in cte_rules.merged_event_values, because the live validator
    does not alias (e.g. reference_document_type never satisfies
    reference_document).

    In-batch duplicate detection is the remaining per-event check and lives
    in :meth:`MockRegEngineService.ingest`, because it needs the batch.
    """
    errors: list[str] = replay_window_errors(event, now=now)

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


def validate_event_like_regengine(
    event: RegEngineEvent, now: datetime | None = None
) -> list[str]:
    """Every reason live RegEngine would refuse this event, in one list.

    This is the full-parity view and deliberately does NOT distinguish
    batch-fatal field constraints from per-event handler rejections, nor
    honour ``REGENGINE_MOCK_EVENT_AGE_MODE`` — callers wanting the boundary
    should use :func:`field_constraint_errors` and
    :func:`handler_rejection_errors`, which is what
    :meth:`MockRegEngineService.ingest` does.
    """
    return [message for _field, message in field_constraint_errors(event, now=now)] + (
        handler_rejection_errors(event, now=now)
    )


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

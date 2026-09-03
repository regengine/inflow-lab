from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, NamedTuple
from uuid import uuid4

import logging

from .cte_rules import REQUIRED_KDES
from .regengine_client import WEBHOOK_HMAC_SECRET_ENV

logger = logging.getLogger("inflow_lab")
from .schemas.domain import DestinationMode, RegEngineEvent
from .schemas.ingestion import IngestPayload, IngestResponseEvent, MockIngestResponse
from .store import EventStore


# Mirrors RegEngine's WebhookPayload constraint: events accepts 1-500 items.
MIN_BATCH_EVENTS = 1
MAX_BATCH_EVENTS = 500
# Mirrors RegEngine's Pydantic timestamp validator: >24h in the future is rejected.
MAX_FUTURE_HOURS = 24
# Mirrors RegEngine's replay-window floor (webhook_router_v2/security.py's
# _validate_event_timestamp_window, WEBHOOK_MAX_EVENT_AGE_DAYS, default 90):
# an event timestamped further in the past than this is rejected with
# "replay window exceeded" (#102). Unlike MAX_FUTURE_HOURS -- a Pydantic
# field_validator that 422s the whole request (#101) -- live RegEngine
# checks this per event *inside* the route handler, so a stale timestamp
# only rejects that one event; the rest of the batch still succeeds. See
# validate_event_like_regengine's `now` parameter and
# MockRegEngineService's `enforce_event_age_window` for how the mock
# applies it.
MAX_EVENT_AGE_DAYS = 90
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


def _verify_webhook_signature(body: bytes | None, signature_header: str | None) -> None:
    """Reject a missing/incorrect X-Webhook-Signature the way live RegEngine does (#113).

    Gated by REGENGINE_WEBHOOK_HMAC_SECRET -- the same env var
    LiveRegEngineClient reads (see regengine_client.py's
    _build_signature_header). The client is the source of truth for
    exactly what gets signed: json.dumps(payload.model_dump(mode="json"),
    separators=(",", ":"), sort_keys=True), sent via httpx content= so the
    signed bytes equal the wire bytes. Verifying against caller-supplied
    raw wire bytes here -- rather than re-serializing the already-parsed
    payload -- is deliberate: re-deriving our own canonical bytes and
    signing those would never catch a body-serialization drift bug in the
    client's signer, which is the exact class of bug this check exists to
    catch (docs/HMAC_STAGING_VALIDATION.md's "Body-Bytes Drift").

    Two no-op cases, both matching live semantics rather than inventing
    new ones:
    - Unset/blank secret: RegEngine's own verifier no-ops in this mode
      (see docs/HMAC_STAGING_VALIDATION.md, "RegEngine Secret Missing")
      and the client skips signing entirely, so nothing is enforced here
      either -- this is the pre-signing migration ramp on both sides.
    - body=None: no wire bytes exist to check against. The HTTP route
      always passes the exact bytes it received; the simulator's
      in-process mock-delivery call (SimulationController._deliver_payload,
      DestinationMode.MOCK) calls ingest() directly on an already-parsed
      payload and never serializes or signs anything, so there is nothing
      to verify on that path.
    """
    secret = os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip()
    if not secret or body is None:
        return

    if not signature_header:
        raise MockRegEngineHTTPError(401, "Missing X-Webhook-Signature header")

    scheme, _, provided_digest = signature_header.partition("=")
    if scheme != "sha256" or not provided_digest:
        raise MockRegEngineHTTPError(
            401, "Unsupported X-Webhook-Signature scheme (expected 'sha256=<hex>')"
        )

    try:
        provided_digest.encode("ascii")
    except UnicodeEncodeError:
        raise MockRegEngineHTTPError(401, "Invalid X-Webhook-Signature")

    expected_digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_digest, provided_digest):
        raise MockRegEngineHTTPError(401, "Invalid X-Webhook-Signature")


class MockRegEngineService:
    """In-process stand-in for RegEngine's POST /api/v1/webhooks/ingest.

    Unlike the original always-accept mock, this mirrors the live webhook's
    validation so the demo experience matches what a customer hits in the
    wild: batch-size bounds, request-fatal field constraints that 422 the
    whole batch (#101), strict per-CTE KDE checks (exact key lookup, no
    aliasing), the location-identifier requirement, in-batch duplicate
    rejection, an opt-in 90-day replay-window floor (#102), 24h idempotency
    replays, and (when a shared secret is configured) HMAC-SHA256 signature
    verification.

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
        enforce_event_age_window: bool = False,
        max_event_age_days: int = MAX_EVENT_AGE_DAYS,
    ) -> None:
        self._chain_hash = _resume_chain_hash(store) if store is not None else ""
        self._idempotency_cache: OrderedDict[str, _CachedIdempotencyEntry] = OrderedDict()
        # Plain instance attributes (not module constants captured at import
        # time) so a test can inject a short TTL and/or a controllable clock
        # to cross the replay boundary deterministically, without sleeping
        # (#120). Production callers get the real 24h window and wall clock.
        self.idempotency_ttl = idempotency_ttl
        self._clock = clock
        self.max_event_age_days = max_event_age_days
        # Off by default -- deliberately, not an oversight (#102). Turning
        # this on unconditionally against the *default* wall-clock `clock`
        # would reject every one of this repo's own fixed-date fixtures the
        # instant real time drifts past them by more than
        # MAX_EVENT_AGE_DAYS: app/demo_fixtures.py's three shipped
        # DemoFixture entries, and the "2026-02-05"-style timestamp dozens
        # of existing tests across the suite use as their canonical valid
        # event, are already >90 days stale as of this writing. That is
        # exactly the "green demo, failing live post" failure this
        # simulator exists to avoid -- just inverted (a demo that now fails
        # on its own bundled data instead of passing data live would
        # reject). The check itself is fully implemented and correct (see
        # validate_event_like_regengine's `now` parameter); a caller that
        # wants it live -- this test suite, or a future caller with a
        # source of non-stale demo data -- opts in explicitly here and
        # supplies `clock=` to control what "now" means for it.
        self._enforce_event_age_window = enforce_event_age_window

    def reset(self) -> None:
        self._chain_hash = ""
        self._idempotency_cache.clear()

    def ingest(
        self,
        payload: IngestPayload,
        idempotency_key: str | None = None,
        friction: tuple[str, ...] = (),
        *,
        raw_body: bytes | None = None,
        signature_header: str | None = None,
    ) -> MockIngestResponse:
        # Signature verification is the outermost gate -- mirroring live
        # RegEngine's own ordering, where a request is authenticated at
        # the transport layer before any application-level handling (the
        # friction codes below simulate *application* failures like a bad
        # API key or an inactive subscription, which is a different
        # concern from "is this request from the holder of the shared
        # secret at all") (#113).
        _verify_webhook_signature(raw_body, signature_header)

        for code in friction:
            failure = FRICTION_RESPONSES.get(code)
            if failure is None:
                raise MockRegEngineHTTPError(
                    400,
                    f"Unknown X-Mock-Friction code {code!r}. "
                    f"Valid codes: {', '.join(sorted(FRICTION_RESPONSES))}",
                )
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

        # #101: traceability_lot_code/product_description/quantity length
        # and range, and the future-timestamp ceiling, are Pydantic
        # Field()/field_validator constraints on RegEngine's real
        # IngestEvent model -- FastAPI evaluates them while parsing the
        # request body, before the route handler (and thus before any
        # idempotency-key or per-event handling) ever runs. A violation
        # anywhere in the batch 422s the ENTIRE request live, not just the
        # offending event, so scan the whole batch up front and fail the
        # same way -- these must never fall into the per-event
        # accepted/rejected split below, which would show the "N accepted,
        # 1 rejected" partial success live loses the whole batch instead.
        field_errors = [
            f"events[{index}]: {message}"
            for index, event in enumerate(payload.events)
            for message in _field_constraint_errors(event)
        ]
        if field_errors:
            raise MockRegEngineHTTPError(422, "; ".join(field_errors))

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

        # #102: the replay-window floor is handler-level in live RegEngine
        # (checked per event, inside the route, after the request body has
        # already passed Pydantic validation above) -- it rejects only the
        # stale event, not the batch. Only fed a real `now` when this
        # instance opted into enforce_event_age_window (reusing the same
        # `now` this call already resolved above, rather than calling
        # self._clock() again); see __init__ for why that is off by
        # default.
        age_check_now = now if self._enforce_event_age_window else None

        response_events: list[IngestResponseEvent] = []
        accepted = 0
        rejected = 0
        out_of_window_events = 0
        seen_batch_keys: set[str] = set()
        for event in payload.events:
            errors = _handler_level_errors(event, now=age_check_now, max_age_days=self.max_event_age_days)
            if not self._enforce_event_age_window:
                age_errors = _handler_level_errors(event, now=now, max_age_days=self.max_event_age_days)
                if any("replay window exceeded" in e for e in age_errors):
                    out_of_window_events += 1
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

        window_mode = "enforced" if self._enforce_event_age_window else "bypassed"
        if out_of_window_events and not self._enforce_event_age_window:
            logger.warning(
                "mock ingest bypassed %d event(s) outside the %d-day replay window "
                "(enforce_event_age_window=False); live RegEngine would reject them",
                out_of_window_events,
                self.max_event_age_days,
            )
        response = MockIngestResponse(
            accepted=accepted,
            rejected=rejected,
            total=accepted + rejected,
            events=response_events,
            ingestion_timestamp=now,
            out_of_window_events=out_of_window_events,
            event_age_window_mode=window_mode,
            event_age_window_days=self.max_event_age_days,
        )
        if idempotency_key:
            self._idempotency_cache[idempotency_key] = _CachedIdempotencyEntry(
                response=response, inserted_at=now
            )
            while len(self._idempotency_cache) > _IDEMPOTENCY_CACHE_LIMIT:
                self._idempotency_cache.popitem(last=False)
        return response


def validate_event_like_regengine(
    event: RegEngineEvent, *, now: datetime | None = None
) -> list[str]:
    """Every check mirroring RegEngine's webhook validation for one event.

    Combines two tiers that live RegEngine draws a hard line between (see
    _field_constraint_errors and _handler_level_errors below) into the one
    flat list this function has always returned, so existing callers that
    just want "is this event valid" for a single event in isolation see no
    change. MockRegEngineService.ingest() itself does NOT call this
    directly -- #101 means it needs the two tiers kept apart (a field
    constraint 422s the whole batch; a handler-level issue rejects only
    this event), so it calls the two halves separately instead.

    Uses the same merged namespace RegEngine builds (top-level fields plus
    the kdes dict) with strict string lookup — deliberately NOT the lenient
    aliasing in cte_rules.merged_event_values, because the live validator
    does not alias (e.g. reference_document_type never satisfies
    reference_document).

    ``now`` is the #102 replay-window floor's reference clock, forwarded
    to _handler_level_errors -- see that function's docstring. Defaults to
    None (age check skipped) so every pre-existing direct caller of this
    function keeps its exact current behavior.
    """
    return _field_constraint_errors(event) + _handler_level_errors(event, now=now, max_age_days=MAX_EVENT_AGE_DAYS)


def _field_constraint_errors(event: RegEngineEvent) -> list[str]:
    """The four checks that are Pydantic Field()/field_validator constraints
    on RegEngine's real IngestEvent model, not application/handler logic --
    traceability_lot_code and product_description length, quantity's
    positivity, and the future-timestamp ceiling (#101). FastAPI evaluates
    these while parsing the request body, before RegEngine's route handler
    runs at all, so live rejects the ENTIRE batch with 422 the instant any
    one event trips one of these -- never a per-event "rejected" result
    alongside other events accepted. MockRegEngineService.ingest() scans
    every event in the batch with this function up front for exactly that
    reason; see its docstring there.

    No `now` parameter, unlike _handler_level_errors: the future-timestamp
    ceiling always compares against the real wall clock, matching this
    function's behavior before #102 split it out. Do not change this to
    accept an injectable `now` without first checking
    tests/test_mock_parity.py's #120 TTL-boundary test, which injects a
    clock deliberately far from its events' fixed timestamps for
    idempotency purposes only and would misfire against this check if it
    started honoring that same clock.
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
    return errors


def _handler_level_errors(event: RegEngineEvent, *, now: datetime | None = None, max_age_days: int = MAX_EVENT_AGE_DAYS) -> list[str]:
    """Checks RegEngine applies per event, inside the route handler, after
    the request body as a whole has already passed Pydantic validation: the
    location-identifier requirement, required KDEs per CTE type, and
    (#102) the 90-day replay-window floor. A violation here rejects only
    this one event -- unlike _field_constraint_errors, the rest of the
    batch is unaffected.

    ``now`` gates the replay-window floor specifically: when None (the
    default), the age check is skipped entirely, matching this codebase's
    behavior before #102. A caller that wants it enforced passes the
    reference time to compare against -- MockRegEngineService.ingest()
    does this only when constructed with enforce_event_age_window=True,
    using its own injectable clock (#120's pattern) rather than a bare
    datetime.now(UTC) call, so a test can cross the boundary deterministically.
    """
    errors: list[str] = []

    timestamp = event.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if now is not None and timestamp < now - timedelta(days=max_age_days):
        errors.append(
            f"timestamp is older than WEBHOOK_MAX_EVENT_AGE_DAYS={max_age_days} "
            "— replay window exceeded"
        )

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

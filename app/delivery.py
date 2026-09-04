"""Delivery mechanics: posting a batch and turning the result into records.

`controller.py` used to own all of this alongside the run loop, the
data-plane lock and scenario saves (#212). Two kinds of thing live here now:

* The POST itself -- `deliver_payload` dials the mock or live RegEngine,
  applies the #95 idempotency-replay check, masks the API key out of every
  failure, and returns one `DeliveryOutcome`. `deliver_in_chunks` slices an
  oversized batch at RegEngine's cap (#103) and posts each slice through a
  caller-supplied deliverer.
* Pure projections over that outcome -- pairing verdicts back onto the
  events that earned them (`pair_event_responses`), deriving per-record
  fields (`event_delivery_fields`, `build_stored_records`) and rolling
  several chunks into one response summary (`aggregate_outcomes`).

Nothing here touches the controller's lock or state. The controller still
decides *when* to deliver and where the snapshot / deliver / commit
boundaries fall (#208 -- the `_store_epoch` discipline stays next to
`self._lock`); this module decides *how* a batch is posted and *what* the
resulting records look like.

`deliver_payload` takes its collaborators as arguments rather than being a
method on something that holds them, deliberately: `SimulationController`
keeps a one-line `_deliver_payload` that forwards `self.mock_service` and
`self.live_client` on every call, so a test that swaps either attribute on
the controller -- or patches `_deliver_payload` itself, which every delivery
path and `deliver_in_chunks` route through -- sees exactly the behaviour it
saw before the split.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

from .mock_service import MockRegEngineHTTPError, MockRegEngineService
from .regengine_client import LiveRegEngineClient, LiveRegEngineDeliveryError
from .schemas.domain import DestinationMode, RegEngineEvent, StoredEventRecord
from .schemas.ingestion import IngestPayload
from .schemas.simulation import SimulationConfig
from .store import mask_secret_in_payload, mask_secret_in_string

# Same name-keyed singleton logger main.py configures, so delivery
# failures land in the stream operators already watch (#182).
logger = logging.getLogger("inflow_lab")

# Mirrored from the response models in app/schemas so a delivery cannot
# hand the API a status its schema rejects -- mypy checks every assignment
# below against this.
DeliveryStatus = Literal["generated", "posted", "failed"]


@dataclass(slots=True)
class DeliveryOutcome:
    response: dict[str, Any] | None = None
    delivery_status: DeliveryStatus = "generated"
    posted: int = 0
    failed: int = 0
    delivery_attempts: int = 0
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None


# What `deliver_in_chunks` posts each slice through. `SimulationController`
# passes its bound `_deliver_payload`, whose extra defaulted
# `idempotency_key` parameter is compatible with this two-argument shape.
Deliverer = Callable[[IngestPayload, SimulationConfig], Awaitable[DeliveryOutcome]]


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def log_delivery_failure(
    mode: str,
    idempotency_key: str | None,
    payload: IngestPayload,
    exc: Exception,
    *,
    tenant_id: str | None,
    api_key: str | None,
    detail: str = "",
) -> None:
    """The one place a delivery failure is written to the log (#182).

    Both failure paths funnel through here rather than formatting their own
    line, for two reasons. The masking is the load-bearing one: the live
    path masked the API key out of the exception text and the mock path did
    not, so whether a raised message could leak the credential depended on
    which destination happened to fail. One choke point makes that
    structural instead of remembered.

    The other is correlation. `tenant`, `mode` and `idempotency_key` are
    what let an operator tie this line to the request the caller saw fail;
    without them the log says a delivery failed but not whose, over which
    destination, or which attempt.
    """
    logger.error(
        "Delivery failed: tenant=%s mode=%s idempotency_key=%s events=%d %s(%s)",
        tenant_id or "unknown",
        mode,
        idempotency_key or "none",
        len(payload.events),
        f"{detail} " if detail else "",
        mask_secret_in_string(str(exc), api_key),
    )


async def deliver_payload(
    mock_service: MockRegEngineService,
    live_client: LiveRegEngineClient,
    payload: IngestPayload,
    config: SimulationConfig,
    *,
    idempotency_key: str | None = None,
    tenant_id: str | None = None,
) -> DeliveryOutcome:
    """POST *payload* to the destination *config* names and report what happened.

    Never raises for a delivery failure: every path returns a
    `DeliveryOutcome`, with the failure logged (masked) and described in
    `error_message`, so the controller's commit phase always has something
    to record against each event.
    """
    if config.delivery.mode == DestinationMode.NONE:
        return DeliveryOutcome()

    api_key = config.delivery.api_key
    attempted_at = datetime.now(UTC)
    delivery_idempotency_key = idempotency_key
    if delivery_idempotency_key is None:
        delivery_idempotency_key = uuid.uuid4().hex
    try:
        if config.delivery.mode == DestinationMode.MOCK:
            response = mock_service.ingest(
                payload,
                idempotency_key=delivery_idempotency_key,
                friction=tuple(config.delivery.mock_friction),
            ).model_dump(mode="json")
            metadata = {
                "delivery_mode": "mock",
                "attempted_event_count": len(payload.events),
                "idempotency_key": delivery_idempotency_key,
            }
        elif config.delivery.mode == DestinationMode.LIVE:
            result = await live_client.ingest(
                payload,
                config,
                idempotency_key=delivery_idempotency_key,
            )
            response = result.response
            metadata = result.metadata | {"attempted_event_count": len(payload.events)}
        else:
            return DeliveryOutcome()

        # #95: a repeated Idempotency-Key makes both the mock and live
        # RegEngine replay a cached response verbatim, without
        # revalidating anything (app/mock_service.py's
        # _idempotency_cache; live RegEngine's own idempotency store
        # behaves the same, per the shared TTL cache both sides pin
        # against). That is correct when the replayed request is the
        # one that populated the cache entry -- but retry_failed_delivery
        # can legitimately resend a *subset* of a larger original batch
        # under that batch's key (see its reuse_original_key comment),
        # so the cached response can describe more events than this
        # request actually sent. Every genuine (non-replayed) response
        # carries exactly one verdict per requested event -- the same
        # invariant pair_event_responses relies on -- so a mismatched
        # count is the tell. Treat that as a replay rather than a
        # delivery: folding a stale batch's accepted/rejected counts into
        # this request's posted/failed would silently overstate what this
        # retry actually accomplished (#95's second bug).
        masked_response = mask_secret_in_payload(response, api_key)
        if _is_idempotency_replay(response, payload):
            return DeliveryOutcome(
                response=masked_response,
                delivery_status="failed",
                posted=0,
                failed=len(payload.events),
                delivery_attempts=1,
                attempted_at=attempted_at,
                completed_at=datetime.now(UTC),
                error_message=(
                    "RegEngine replayed a cached idempotency response sized "
                    "for a different request; none of this request's events "
                    "were revalidated. Retry again to mint a fresh key."
                ),
                metadata=metadata | {"idempotency_replay": True},
            )
        return DeliveryOutcome(
            response=masked_response,
            delivery_status="posted",
            posted=response.get("accepted", len(payload.events)),
            failed=response.get("rejected", 0),
            delivery_attempts=1,
            attempted_at=attempted_at,
            completed_at=datetime.now(UTC),
            metadata=metadata,
        )
    except MockRegEngineHTTPError as exc:
        log_delivery_failure(
            config.delivery.mode.value,
            delivery_idempotency_key,
            payload,
            exc,
            tenant_id=tenant_id,
            api_key=api_key,
            detail=f"rejected with HTTP {exc.status_code}",
        )
        return DeliveryOutcome(
            delivery_status="failed",
            failed=len(payload.events),
            delivery_attempts=1,
            attempted_at=attempted_at,
            error_message=str(exc),
            metadata={
                "delivery_mode": "mock",
                "attempted_event_count": len(payload.events),
                "status_code": exc.status_code,
                "idempotency_key": delivery_idempotency_key,
            },
        )
    except LiveRegEngineDeliveryError as exc:
        # Masked: the raw exception can quote the request, and the API key
        # rides in a header. Host, not full URL, for the same reason.
        log_delivery_failure(
            config.delivery.mode.value,
            delivery_idempotency_key,
            payload,
            exc,
            tenant_id=tenant_id,
            api_key=api_key,
            # Host, not full URL: the path can carry a token too.
            detail=f"host={urlparse(str(config.delivery.endpoint or '')).hostname or 'unknown host'}",
        )
        metadata = exc.metadata | {"attempted_event_count": len(payload.events)}
        if delivery_idempotency_key and "idempotency_key" not in metadata:
            metadata["idempotency_key"] = delivery_idempotency_key
        return DeliveryOutcome(
            delivery_status="failed",
            failed=len(payload.events),
            delivery_attempts=1,
            attempted_at=attempted_at,
            error_message=mask_secret_in_string(str(exc), api_key),
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - exercised by live integration, not unit tests
        logger.exception(
            "unexpected %s delivery failure: %s events not posted (%s)",
            config.delivery.mode.value,
            len(payload.events),
            mask_secret_in_string(str(exc), api_key),
        )
        metadata = {
            "delivery_mode": config.delivery.mode.value,
            "attempted_event_count": len(payload.events),
        }
        if delivery_idempotency_key:
            metadata["idempotency_key"] = delivery_idempotency_key
        return DeliveryOutcome(
            delivery_status="failed",
            failed=len(payload.events),
            delivery_attempts=1,
            attempted_at=attempted_at,
            error_message=mask_secret_in_string(str(exc), api_key),
            metadata=metadata,
        )


async def deliver_in_chunks(
    deliver: Deliverer,
    source: str,
    events: list[RegEngineEvent],
    config: SimulationConfig,
    *,
    chunk_size: int,
) -> list[tuple[list[RegEngineEvent], DeliveryOutcome]]:
    """POST *events* through *deliver* in slices of at most *chunk_size* (#103).

    RegEngine's real webhook route -- and the mock mirroring it, see
    app/mock_service.py's MAX_BATCH_EVENTS -- rejects the entire
    request with one 422 the instant a batch exceeds this cap.
    import_csv and replay are the two callers that can hand this more
    events than that: neither bounds how many CSV rows get parsed or
    how large a persisted store has grown before building one
    IngestPayload (step() cannot -- batch_size caps at 100 -- and
    retry_failed_delivery cannot either, since DeliveryRetryRequest.limit
    caps at 500, this same constant's value).

    The cap is a parameter rather than read here so the controller passes
    its own module's `MAX_BATCH_EVENTS` -- the name tests shrink to force
    several small chunks without touching the mock's real cap.

    Each chunk goes out as its own request via *deliver*, which mints it
    a fresh Idempotency-Key exactly as it already does for any caller
    that passes none -- from RegEngine's side a chunk is a wholly
    separate request, never a retry of another one.

    Returns each chunk's own event slice paired with its own
    DeliveryOutcome, in order, so a caller that must pair per-event
    verdicts back onto records (import_csv, via build_stored_records)
    can zip each outcome against exactly the events that produced it
    instead of re-deriving chunk boundaries a second time. A call with
    `events` at or under the cap produces exactly one chunk covering
    the whole list -- the common case, and aggregate_outcomes passes
    a single outcome straight through unchanged, so it behaves
    identically to the unchunked call this replaces.
    """
    chunks: list[tuple[list[RegEngineEvent], DeliveryOutcome]] = []
    for start in range(0, len(events), chunk_size):
        chunk_events = events[start : start + chunk_size]
        payload = IngestPayload(source=source, events=chunk_events)
        outcome = await deliver(payload, config)
        chunks.append((chunk_events, outcome))
    return chunks


def _is_idempotency_replay(response: dict[str, Any], payload: IngestPayload) -> bool:
    """True when *response* answers a differently-sized request than *payload* (#95).

    A genuinely fresh RegEngine response -- mock or live -- carries
    exactly one verdict per submitted event (MockRegEngineService.ingest
    appends one IngestResponseEvent per loop iteration over
    payload.events; live RegEngine's contract is the same). A repeated
    Idempotency-Key instead replays whatever response that key originally
    cached, verbatim and without revalidating anything -- correct when
    the replayed request is byte-for-byte the one that populated the
    cache, but wrong when it is only a subset (retry_failed_delivery
    resending fewer events than the original batch that owned the key).
    The subset case is exactly what a mismatched event count catches:
    nothing about a legitimate, matching replay ever trips it.
    """
    return len(response.get("events", [])) != len(payload.events)


# ---------------------------------------------------------------------------
# From an outcome to records
# ---------------------------------------------------------------------------


def stored_idempotency_key(record: StoredEventRecord) -> str | None:
    metadata = record.delivery_metadata or {}
    value = metadata.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None


def pair_event_responses(
    events: list[Any],
    response_events: list[Any],
) -> list[dict[str, Any] | None]:
    """Match each sent event to its own verdict, keyed on identity.

    RegEngine does not answer in request order. Its webhook route builds
    the response in phases — replay-window and KDE rejections first, then
    rules-enforcement rejections, then every acceptance — so the response
    list is partitioned rejected-first. Zipping it against the request by
    array index therefore attaches one event's verdict to a different
    event whenever a rejection follows an acceptance: a valid lot is
    stored as ``failed`` carrying another lot's validation errors, and the
    ``event_id``/``sha256_hash``/``chain_hash`` of a real accepted event
    are copied onto the wrong record. Those hashes are the evidence.

    ``(traceability_lot_code, cte_type)`` is on the wire on both sides and
    is the join key. A key can legitimately repeat inside one batch — the
    same lot and CTE at two different timestamps — so responses are held
    in a per-key queue and consumed in order rather than collapsed into a
    dict. Within a partition RegEngine preserves request order, so the
    Nth event with a given key gets the Nth response with that key.

    A key with no response left is ``None``: no verdict. Callers must
    treat that as "unknown", never fall back to positional matching —
    a wrong verdict is worse than an absent one.

    Note this is invisible to the unit suite by construction:
    ``mock_service`` appends accepted and rejected in a single pass, so
    the mock's response *is* in request order.
    """
    by_key: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for response in response_events:
        if not isinstance(response, dict):
            continue
        lot_code = response.get("traceability_lot_code")
        cte_type = response.get("cte_type")
        if lot_code is None or cte_type is None:
            continue
        by_key[(str(lot_code), str(cte_type))].append(response)

    paired: list[dict[str, Any] | None] = []
    for event in events:
        cte_type = getattr(event.cte_type, "value", event.cte_type)
        queue = by_key.get((str(event.traceability_lot_code), str(cte_type)))
        paired.append(queue.popleft() if queue else None)
    return paired


def event_delivery_fields(outcome: DeliveryOutcome, event_response: dict[str, Any] | None) -> dict[str, Any]:
    """Per-record delivery fields honoring per-event rejection.

    RegEngine (and the mock mirroring it) returns HTTP 2xx with per-event
    accepted/rejected statuses; a rejected event was not ingested, so its
    stored record must read as failed with the validator's errors.
    """
    status = outcome.delivery_status
    error = outcome.error_message
    if (
        status == "posted"
        and isinstance(event_response, dict)
        and event_response.get("status") == "rejected"
    ):
        status = "failed"
        errors = event_response.get("errors") or []
        error = "; ".join(str(item) for item in errors) or "Event rejected by ingest validation"
    return {
        "delivery_status": status,
        "last_delivery_success_at": outcome.completed_at if status == "posted" else None,
        "error": error,
    }


def build_stored_records(
    source: str,
    events: list[RegEngineEvent],
    parent_lot_codes: list[list[str]],
    delivery_mode: DestinationMode,
    outcome: DeliveryOutcome,
) -> list[StoredEventRecord]:
    """Build one fresh ``StoredEventRecord`` per event from one ``DeliveryOutcome`` (#165).

    ``step``, ``import_csv``, and ``load_demo_fixture`` each post a freshly
    generated batch of events through a single ``DeliveryOutcome`` and need
    the same event-to-response pairing (``pair_event_responses``) plus
    per-event field derivation (``event_delivery_fields``) before
    persisting -- this was copy-pasted three times with only the
    source/events/parent_lot_codes/delivery_mode inputs differing per call
    site, risking silent drift between the copies.

    ``events[i]`` and ``parent_lot_codes[i]`` must correspond to the same
    event -- callers zip their own per-event lineage against their own
    event list before calling in, since where that lineage comes from
    (engine.next_event, CSV parsing, a demo fixture) differs per call site.

    ``retry_failed_delivery`` does NOT go through this helper: it patches
    *existing* records via ``model_copy`` and adds the new outcome's
    ``delivery_attempts`` onto each record's prior count instead of
    building fresh records, and does its own per-record idempotency-key
    grouping besides -- differently shaped enough that forcing it through
    here would trade the duplication this removes for a pile of
    conditionals undoing most of what the helper buys.
    """
    response_events = (outcome.response or {}).get("events", []) if outcome.response else []
    paired_responses = pair_event_responses(events, response_events)
    stored_records: list[StoredEventRecord] = []
    for index, event in enumerate(events):
        event_response = paired_responses[index]
        stored_records.append(
            StoredEventRecord(
                payload_source=source,
                event=event,
                parent_lot_codes=parent_lot_codes[index],
                destination_mode=delivery_mode,
                delivery_attempts=outcome.delivery_attempts,
                last_delivery_attempt_at=outcome.attempted_at,
                delivery_response=event_response,
                delivery_metadata=outcome.metadata,
                **event_delivery_fields(outcome, event_response),
            )
        )
    return stored_records


def aggregate_outcomes(outcomes: list[DeliveryOutcome]) -> DeliveryOutcome:
    """Roll up one DeliveryOutcome per chunk into the single summary
    import_csv/replay's response schemas need (#103).

    A single-chunk call -- everything at or under MAX_BATCH_EVENTS, still
    the overwhelming common case -- passes its one outcome through
    completely unchanged rather than rebuild it into a synthetic shape:
    CSVImportResponse.response / ReplayResponse.response stays exactly
    whatever mock_service/live_client actually returned (including
    fields like "total"/"ingestion_timestamp" a merged dict would
    otherwise drop), and every other field is that same single outcome's
    own.

    Multiple chunks combine by summing posted/failed/delivery_attempts,
    so an early chunk succeeding is never erased by a later one failing
    -- the concrete "partial failure mid-way through several chunks"
    case #103 asks for. delivery_status collapses to "failed" if *any*
    chunk failed outright (surfaced by callers as
    status="delivery_failed"/"failed"), to "generated" only when every
    chunk was (delivery mode NONE never calls RegEngine at all), and to
    "posted" otherwise -- the exact three values ReplayResponse's own
    ``{"posted": ..., "failed": ..., "generated": ...}[outcome.delivery_status]``
    lookup requires; see SimulationController.replay().

    This aggregate is for the one response object each caller returns,
    never for per-record fields -- building StoredEventRecords must use
    each chunk's own outcome instead (its own delivery_attempts, its own
    response for verdict pairing), which is exactly why import_csv calls
    build_stored_records once per chunk rather than against this.
    """
    if not outcomes:
        return DeliveryOutcome()
    if len(outcomes) == 1:
        return outcomes[0]

    delivery_status: DeliveryStatus
    if any(outcome.delivery_status == "failed" for outcome in outcomes):
        delivery_status = "failed"
    elif all(outcome.delivery_status == "generated" for outcome in outcomes):
        delivery_status = "generated"
    else:
        delivery_status = "posted"

    error_messages = [outcome.error_message for outcome in outcomes if outcome.error_message]

    response_events: list[Any] = []
    any_response = False
    for outcome in outcomes:
        if outcome.response is not None:
            any_response = True
            response_events.extend(outcome.response.get("events", []))

    posted = sum(outcome.posted for outcome in outcomes)
    failed = sum(outcome.failed for outcome in outcomes)
    return DeliveryOutcome(
        response=(
            {
                "chunk_count": len(outcomes),
                "accepted": posted,
                "rejected": failed,
                "events": response_events,
            }
            if any_response
            else None
        ),
        delivery_status=delivery_status,
        posted=posted,
        failed=failed,
        delivery_attempts=sum(outcome.delivery_attempts for outcome in outcomes),
        error_message="; ".join(error_messages) or None,
    )

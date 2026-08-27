"""Turning a delivery attempt into stored evidence records.

`controller.py` used to own this alongside the run loop, scenario saves and
integration config. The three generation paths (`step`, `import_csv`,
`load_demo_fixture`) each rebuilt the same pair-then-loop block by hand, so
any change to how a delivery outcome becomes a stored record had to be made
in three places -- four, counting the differently shaped retry variant --
and could silently drift between them.

Everything here is pure: no locks, no I/O, no controller state. The
controller decides *when* to deliver and where the lock boundaries fall;
this module decides *what* the resulting records look like.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence

from .mock_service import MAX_BATCH_EVENTS, MockRegEngineHTTPError
from .regengine_client import LiveRegEngineDeliveryError
from .schemas.domain import DestinationMode, StoredEventRecord
from .schemas.ingestion import IngestPayload
from .schemas.simulation import SimulationConfig
from .store import mask_secret_in_payload, mask_secret_in_string


@dataclass(slots=True)
class DeliveryOutcome:
    response: dict[str, Any] | None = None
    delivery_status: str = "generated"
    posted: int = 0
    failed: int = 0
    delivery_attempts: int = 0
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None


def response_events(outcome: DeliveryOutcome) -> list[Any]:
    """The per-event verdict list from a delivery response, if there is one."""
    if not outcome.response:
        return []
    return outcome.response.get("events", [])


def pair_event_responses(
    events: Sequence[Any],
    response_events: Sequence[Any],
) -> list[dict[str, Any] | None]:
    """Match each sent event to its own verdict, keyed on identity.

    RegEngine does not answer in request order. Its webhook route builds
    the response in phases -- replay-window and KDE rejections first, then
    rules-enforcement rejections, then every acceptance -- so the response
    list is partitioned rejected-first. Zipping it against the request by
    array index therefore attaches one event's verdict to a different
    event whenever a rejection follows an acceptance: a valid lot is
    stored as ``failed`` carrying another lot's validation errors, and the
    ``event_id``/``sha256_hash``/``chain_hash`` of a real accepted event
    are copied onto the wrong record. Those hashes are the evidence.

    ``(traceability_lot_code, cte_type)`` is on the wire on both sides and
    is the join key. A key can legitimately repeat inside one batch -- the
    same lot and CTE at two different timestamps -- so responses are held
    in a per-key queue and consumed in order rather than collapsed into a
    dict. Within a partition RegEngine preserves request order, so the
    Nth event with a given key gets the Nth response with that key.

    A key with no response left is ``None``: no verdict. Callers must
    treat that as "unknown", never fall back to positional matching --
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


def event_delivery_fields(
    outcome: DeliveryOutcome, event_response: dict[str, Any] | None
) -> dict[str, Any]:
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
    *,
    source: str,
    events: Sequence[Any],
    parent_lot_codes: Sequence[Sequence[str]],
    delivery_mode: DestinationMode,
    outcome: DeliveryOutcome,
) -> list[StoredEventRecord]:
    """Build one `StoredEventRecord` per delivered event.

    Shared by `step`, `import_csv` and `load_demo_fixture`; they differ only
    in where the events and their lineage come from.
    """
    paired_responses = pair_event_responses(events, response_events(outcome))
    records: list[StoredEventRecord] = []
    for index, event in enumerate(events):
        event_response = paired_responses[index]
        records.append(
            StoredEventRecord(
                payload_source=source,
                event=event,
                parent_lot_codes=list(parent_lot_codes[index]),
                destination_mode=delivery_mode,
                delivery_attempts=outcome.delivery_attempts,
                last_delivery_attempt_at=outcome.attempted_at,
                delivery_response=event_response,
                delivery_metadata=outcome.metadata,
                **event_delivery_fields(outcome, event_response),
            )
        )
    return records


def rebuild_retried_records(
    *,
    records: Sequence[StoredEventRecord],
    delivery_mode: DestinationMode,
    outcome: DeliveryOutcome,
) -> list[StoredEventRecord]:
    """Patch already-stored records with the result of a retry attempt.

    The retry variant differs from `build_stored_records` in three ways that
    are easy to lose if it is written inline next to the others:
    `delivery_attempts` *accumulates* onto the prior count rather than
    replacing it; a newly successful retry clears the stale `error`; and a
    still-failing retry preserves the record's earlier
    `last_delivery_success_at` instead of blanking it.
    """
    paired_responses = pair_event_responses(
        [record.event for record in records], response_events(outcome)
    )
    updated: list[StoredEventRecord] = []
    for index, record in enumerate(records):
        event_response = paired_responses[index]
        fields = event_delivery_fields(outcome, event_response)
        if fields["delivery_status"] == "posted":
            fields["error"] = None
        else:
            fields["last_delivery_success_at"] = record.last_delivery_success_at
        updated.append(
            record.model_copy(
                update={
                    "destination_mode": delivery_mode,
                    "delivery_attempts": record.delivery_attempts + outcome.delivery_attempts,
                    "last_delivery_attempt_at": outcome.attempted_at
                    or record.last_delivery_attempt_at,
                    "delivery_response": event_response,
                    "delivery_metadata": outcome.metadata,
                    **fields,
                },
                deep=True,
            )
        )
    return updated


def chunk_events(events: Sequence[Any], size: int = MAX_BATCH_EVENTS) -> list[list[Any]]:
    """Split a batch into slices RegEngine will accept.

    ``WebhookPayload.events`` is capped at 500 on both the live receiver and
    the mock, and the bulk senders (CSV import, replay) build one payload
    from every row. Above the cap the whole batch used to 422 and every row
    was recorded failed (#103). Each slice is delivered as its own request
    with its own idempotency key.
    """
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    return [list(events[index : index + size]) for index in range(0, len(events), size)]


def aggregate_outcomes(outcomes: Sequence[DeliveryOutcome]) -> DeliveryOutcome:
    """Fold per-chunk outcomes into the one a caller reports.

    Only the *summary* is aggregated. Stored records are always built from
    the chunk that actually carried them, so each record keeps its own
    idempotency key and its own per-event verdict; merging those here would
    re-introduce exactly the cross-batch confusion #95 is about.
    """
    if not outcomes:
        return DeliveryOutcome()
    if len(outcomes) == 1:
        return outcomes[0]

    statuses = {outcome.delivery_status for outcome in outcomes}
    if statuses == {"generated"}:
        status = "generated"
    elif "failed" in statuses:
        status = "failed"
    else:
        status = "posted"

    responses = [outcome.response for outcome in outcomes if outcome.response]
    merged_response: dict[str, Any] | None = None
    if responses:
        merged_events: list[Any] = []
        for response in responses:
            merged_events.extend(response.get("events", []))
        merged_response = {
            "accepted": sum(int(response.get("accepted", 0) or 0) for response in responses),
            "rejected": sum(int(response.get("rejected", 0) or 0) for response in responses),
            "total": sum(int(response.get("total", 0) or 0) for response in responses),
            "events": merged_events,
            "chunks": len(outcomes),
        }

    return DeliveryOutcome(
        response=merged_response,
        delivery_status=status,
        posted=sum(outcome.posted for outcome in outcomes),
        failed=sum(outcome.failed for outcome in outcomes),
        delivery_attempts=sum(outcome.delivery_attempts for outcome in outcomes),
        attempted_at=next(
            (outcome.attempted_at for outcome in outcomes if outcome.attempted_at), None
        ),
        completed_at=next(
            (outcome.completed_at for outcome in reversed(outcomes) if outcome.completed_at),
            None,
        ),
        error_message=next(
            (outcome.error_message for outcome in outcomes if outcome.error_message), None
        ),
        metadata={
            "chunk_count": len(outcomes),
            "idempotency_keys": [
                (outcome.metadata or {}).get("idempotency_key") for outcome in outcomes
            ],
        },
    )


def stored_idempotency_key(record: StoredEventRecord) -> str | None:
    """The `Idempotency-Key` a stored record was delivered under, if any."""
    metadata = record.delivery_metadata or {}
    value = metadata.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None


def retry_idempotency_key(record: StoredEventRecord) -> str | None:
    """The key a retry of `record` should carry, or None to mint a fresh one.

    A reused `Idempotency-Key` is answered from RegEngine's 24h cache
    without revalidation, so replaying the original key can never redeliver
    an event the server rejected per-event on a 2xx response -- the record
    stays `failed` however many times an operator clicks retry, and the
    cached batch's `accepted` count is folded into the retry's total (#95).

    The key is therefore reused only for the case it exists to protect: an
    attempt that produced no per-event verdict at all (a transport failure,
    where the original request may or may not have reached the server, and
    replay-safety is what stops a double ingest). A record carrying a
    verdict was answered, so its retry is a genuinely new request and gets
    a new key.
    """
    if record.delivery_response is not None:
        return None
    return stored_idempotency_key(record)


IDEMPOTENCY_REPLAY_ERROR = (
    "RegEngine answered this request from its idempotency cache: the response "
    "describes the original batch, so nothing was re-validated or re-ingested."
)


def _is_idempotency_replay(
    response: dict[str, Any], payload: IngestPayload, *, reused_key: bool
) -> bool:
    """Whether a response describes a different batch than the one just sent.

    A reused ``Idempotency-Key`` is answered from cache verbatim, without
    revalidation, for 24h on both the mock and live RegEngine. Counting that
    cached answer as a fresh delivery credits one batch's ``accepted`` total
    to another request (#95). The event count is the tell: a genuine
    response carries one verdict per event sent.

    Two deliberate narrowings keep this from firing on honest responses. A
    freshly minted key cannot hit any cache, so only a key the caller
    supplied is checked at all; and a response carrying *no* per-event
    verdicts is a shape some receivers legitimately return, not evidence of
    a replay.
    """
    if not reused_key:
        return False
    events = response.get("events")
    if not isinstance(events, list) or not events:
        return False
    return len(events) != len(payload.events)


def _replayed_outcome(
    *,
    payload: IngestPayload,
    response: dict[str, Any],
    delivery_mode: str,
    idempotency_key: str | None,
    attempted_at: datetime,
    extra_metadata: dict[str, Any] | None = None,
) -> DeliveryOutcome:
    """A replay is a non-delivery: reported, never counted as posted."""
    metadata: dict[str, Any] = {
        "delivery_mode": delivery_mode,
        "attempted_event_count": len(payload.events),
        "idempotency_key": idempotency_key,
        "idempotency_replay": True,
    }
    if extra_metadata:
        metadata = {**extra_metadata, **metadata}
    return DeliveryOutcome(
        response=response,
        delivery_status="failed",
        posted=0,
        failed=len(payload.events),
        delivery_attempts=1,
        attempted_at=attempted_at,
        completed_at=datetime.now(UTC),
        error_message=IDEMPOTENCY_REPLAY_ERROR,
        metadata=metadata,
    )


async def deliver_payload(
    payload: IngestPayload,
    config: SimulationConfig,
    *,
    mock_service: Any,
    live_client: Any,
    idempotency_key: str | None = None,
) -> DeliveryOutcome:
    """Deliver one payload to the configured destination.

    Never raises for a delivery failure: every failure mode is folded into a
    `DeliveryOutcome` with ``delivery_status="failed"`` so the caller can
    still persist the attempt as evidence. Error text and echoed responses
    are masked against the configured API key before they leave here.
    """
    if config.delivery.mode == DestinationMode.NONE:
        return DeliveryOutcome()

    api_key = config.delivery.api_key
    attempted_at = datetime.now(UTC)
    delivery_idempotency_key = idempotency_key
    if config.delivery.mode != DestinationMode.NONE and delivery_idempotency_key is None:
        delivery_idempotency_key = uuid.uuid4().hex
    try:
        if config.delivery.mode == DestinationMode.MOCK:
            response = mock_service.ingest(
                payload,
                idempotency_key=delivery_idempotency_key,
                friction=tuple(config.delivery.mock_friction),
            ).model_dump(mode="json")
            if _is_idempotency_replay(response, payload, reused_key=idempotency_key is not None):
                return _replayed_outcome(
                    payload=payload,
                    response=mask_secret_in_payload(response, api_key),
                    delivery_mode="mock",
                    idempotency_key=delivery_idempotency_key,
                    attempted_at=attempted_at,
                )
            return DeliveryOutcome(
                response=mask_secret_in_payload(response, api_key),
                delivery_status="posted",
                posted=response.get("accepted", len(payload.events)),
                failed=response.get("rejected", 0),
                delivery_attempts=1,
                attempted_at=attempted_at,
                completed_at=datetime.now(UTC),
                metadata={
                    "delivery_mode": "mock",
                    "attempted_event_count": len(payload.events),
                    "idempotency_key": delivery_idempotency_key,
                },
            )
        if config.delivery.mode == DestinationMode.LIVE:
            result = await live_client.ingest(
                payload,
                config,
                idempotency_key=delivery_idempotency_key,
            )
            if _is_idempotency_replay(
                result.response, payload, reused_key=idempotency_key is not None
            ):
                return _replayed_outcome(
                    payload=payload,
                    response=mask_secret_in_payload(result.response, api_key),
                    delivery_mode="live",
                    idempotency_key=delivery_idempotency_key,
                    attempted_at=attempted_at,
                    extra_metadata=dict(result.metadata),
                )
            return DeliveryOutcome(
                response=mask_secret_in_payload(result.response, api_key),
                delivery_status="posted",
                posted=result.response.get("accepted", len(payload.events)),
                failed=result.response.get("rejected", 0),
                delivery_attempts=1,
                attempted_at=attempted_at,
                completed_at=datetime.now(UTC),
                metadata=result.metadata | {"attempted_event_count": len(payload.events)},
            )
        return DeliveryOutcome()
    except MockRegEngineHTTPError as exc:
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

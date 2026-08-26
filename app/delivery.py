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

from .mock_service import MockRegEngineHTTPError
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

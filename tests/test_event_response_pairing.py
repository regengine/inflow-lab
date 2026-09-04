"""Per-event verdicts must be matched by identity, not by array position.

RegEngine does not answer in request order. Its webhook route appends
replay-window and KDE rejections first, then rules-enforcement rejections,
then every acceptance — so the ``events`` list in the response is
partitioned rejected-first. Inflow Lab used to zip that list against the
request by array index, which attaches one event's verdict to a different
event as soon as a rejection follows an acceptance.

The consequences are not cosmetic. ``event_delivery_fields`` flips a
record to ``delivery_status="failed"`` carrying another lot's validation
errors, and ``event_id``/``sha256_hash``/``chain_hash`` — the evidence —
get copied onto the wrong lot.

Why the rest of the suite cannot see this: every other test delivers
through ``app.mock_service``, which appends accepted and rejected in a
single pass, so the mock's response *is* in request order. These tests
supply a genuinely partitioned response instead.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.delivery import DeliveryOutcome, pair_event_responses
from app.main import controller
from app.schemas.domain import CTEType
from app.schemas.simulation import SimulationConfig


def _response(lot_code: str, cte_type: str, status: str, **extra) -> dict:
    return {
        "traceability_lot_code": lot_code,
        "cte_type": cte_type,
        "status": status,
        **extra,
    }


class _Event:
    """Minimal stand-in carrying only the two fields the join key uses."""

    def __init__(self, lot_code: str, cte_type: CTEType) -> None:
        self.traceability_lot_code = lot_code
        self.cte_type = cte_type


def test_rejected_first_partition_attaches_each_verdict_to_its_own_event():
    """The exact shape RegEngine returns for a mixed batch.

    Sent: A (valid), B (invalid), C (valid). Returned: B's rejection
    first, then A's and C's acceptances. Index-zipping hands A the
    rejection meant for B.
    """
    events = [
        _Event("LOT-A", CTEType.HARVESTING),
        _Event("LOT-B", CTEType.COOLING),
        _Event("LOT-C", CTEType.SHIPPING),
    ]
    response_events = [
        _response("LOT-B", "cooling", "rejected", errors=["missing KDE: location"]),
        _response("LOT-A", "harvesting", "accepted", event_id="evt-a", sha256_hash="sha-a"),
        _response("LOT-C", "shipping", "accepted", event_id="evt-c", sha256_hash="sha-c"),
    ]

    paired = pair_event_responses(events, response_events)

    assert [entry["traceability_lot_code"] for entry in paired] == ["LOT-A", "LOT-B", "LOT-C"]
    assert paired[0]["status"] == "accepted" and paired[0]["event_id"] == "evt-a"
    assert paired[1]["status"] == "rejected"
    assert paired[2]["event_id"] == "evt-c"


def test_repeated_join_key_within_one_batch_is_consumed_in_order():
    """The same lot and CTE can appear twice in a batch at two timestamps.

    A plain dict would collapse them and hand both events the same
    verdict. Responses are queued per key and consumed in order instead.
    """
    events = [
        _Event("LOT-DUP", CTEType.HARVESTING),
        _Event("LOT-DUP", CTEType.HARVESTING),
    ]
    response_events = [
        _response("LOT-DUP", "harvesting", "accepted", event_id="evt-first"),
        _response("LOT-DUP", "harvesting", "accepted", event_id="evt-second"),
    ]

    paired = pair_event_responses(events, response_events)

    assert [entry["event_id"] for entry in paired] == ["evt-first", "evt-second"]


def test_absent_verdict_is_none_and_never_falls_back_to_position():
    """A missing verdict must read as "unknown", not as a neighbour's.

    Positional fallback is what made the original defect silent: there was
    always *some* answer, so nothing ever looked wrong.
    """
    events = [
        _Event("LOT-A", CTEType.HARVESTING),
        _Event("LOT-MISSING", CTEType.COOLING),
    ]
    response_events = [_response("LOT-A", "harvesting", "accepted", event_id="evt-a")]

    paired = pair_event_responses(events, response_events)

    assert paired[0]["event_id"] == "evt-a"
    assert paired[1] is None


def test_step_does_not_mark_a_valid_lot_failed_from_another_lots_rejection(monkeypatch):
    """End-to-end through ``step()``: the wiring, not just the helper.

    Delivery is stubbed to answer the way real RegEngine does — rejection
    first — while the generated events keep their own lot codes. Every
    stored record must carry its own verdict.
    """
    asyncio.run(controller.reset(SimulationConfig()))

    captured: dict[str, list] = {}

    async def fake_deliver(payload, config, idempotency_key=None):
        sent = list(payload.events)
        captured["sent"] = sent
        # Reject the LAST event, and return that rejection FIRST — the
        # partition order real RegEngine produces and the mock never does.
        rejected = sent[-1]
        accepted = sent[:-1]
        response_events = [
            _response(
                rejected.traceability_lot_code,
                rejected.cte_type.value,
                "rejected",
                errors=["missing KDE: location_gln"],
            )
        ] + [
            _response(
                event.traceability_lot_code,
                event.cte_type.value,
                "accepted",
                event_id=f"evt-{index}",
                sha256_hash=f"sha-{index}",
            )
            for index, event in enumerate(accepted)
        ]
        return DeliveryOutcome(
            response={
                "accepted": len(accepted),
                "rejected": 1,
                "events": response_events,
            },
            delivery_status="posted",
            posted=len(sent),
            delivery_attempts=1,
            attempted_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(controller, "_deliver_payload", fake_deliver)
    asyncio.run(controller.step(batch_size=3))

    rejected_lot = captured["sent"][-1].traceability_lot_code
    stored = {
        record.event.traceability_lot_code: record
        for record in controller.store.recent(limit=10)
    }

    assert stored[rejected_lot].delivery_status == "failed"
    assert "location_gln" in (stored[rejected_lot].error or "")

    for lot_code, record in stored.items():
        if lot_code == rejected_lot:
            continue
        assert record.delivery_status == "posted", (
            f"{lot_code} was accepted but stored as {record.delivery_status} "
            f"carrying another lot's error: {record.error!r}"
        )
        assert record.error is None
        # The attached verdict must be this record's own — the copied
        # ``event_id``/``sha256_hash`` are the evidence for this lot.
        assert record.delivery_response is not None
        assert record.delivery_response["traceability_lot_code"] == lot_code

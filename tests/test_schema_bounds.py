"""Model-level bounds on the ingest payload contract.

Earlier work closed #98 only at the edges: the CSV importer rejects
non-finite quantities (`app/csv_importer.py::_parse_quantity`) and the event
store writes with `allow_nan=False` (`app/store.py`). Neither helps a
producer that builds a `RegEngineEvent` directly -- the engine, a demo
fixture, a hand-written payload, a future importer -- and then delivers it
live. These tests pin the two places the guard now lives:

* `RegEngineEvent.quantity` carries `allow_inf_nan=False`, so a non-finite
  quantity cannot be constructed through validation at all.
* `LiveRegEngineClient` serializes the signed body with `allow_nan=False`, so
  even a model built with `model_construct` (validation skipped) fails
  locally instead of putting the bare token `NaN` -- which is not JSON -- on
  a signed, already-sent request.

They also pin that these tightenings changed nothing about the wire shape
AGENTS.md calls non-negotiable.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.delivery import chunk_events
from app.mock_service import MAX_BATCH_EVENTS
from app.regengine_client import (
    INPUT_LOT_CODES_FIELD,
    LiveRegEngineClient,
    build_wire_body,
)
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import DeliveryConfig, DestinationMode, SimulationConfig


TIMESTAMP = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)


def event_kwargs(**overrides):
    kwargs = {
        "cte_type": CTEType.RECEIVING,
        "traceability_lot_code": "TLC-BOUNDS-0001",
        "product_description": "Romaine Hearts",
        "quantity": 120.5,
        "unit_of_measure": "cases",
        "location_name": "Valley Fresh DC",
        "timestamp": TIMESTAMP,
        "kdes": {"receiving_location": "Valley Fresh DC"},
    }
    kwargs.update(overrides)
    return kwargs


# --- #98: non-finite quantities are rejected at the model ---------------------


@pytest.mark.parametrize(
    "quantity",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "-inf"],
)
def test_non_finite_quantity_is_rejected_by_the_event_model(quantity: float) -> None:
    with pytest.raises(ValidationError) as excinfo:
        RegEngineEvent(**event_kwargs(quantity=quantity))
    assert "quantity" in str(excinfo.value)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_quantity_is_rejected_when_it_arrives_as_a_string(literal: str) -> None:
    """Pydantic coerces these strings to floats, so the guard must catch them too."""
    with pytest.raises(ValidationError):
        RegEngineEvent.model_validate(event_kwargs(quantity=literal))


def test_non_finite_quantity_is_rejected_through_the_ingest_payload() -> None:
    """The whole batch fails validation, not just the offending event."""
    with pytest.raises(ValidationError) as excinfo:
        IngestPayload.model_validate(
            {
                "source": "schema-bounds",
                "events": [event_kwargs(), event_kwargs(quantity=float("nan"))],
            }
        )
    assert "events.1.quantity" in str(excinfo.value)


def test_ordinary_quantities_still_validate() -> None:
    """The guard must not narrow the range of quantities the engine produces."""
    for quantity in (0.01, 1.0, 24.0, 6200.0, 1_000_000.5):
        assert RegEngineEvent(**event_kwargs(quantity=quantity)).quantity == quantity


# --- #98: the signed wire body never carries a bare NaN token -----------------


def unvalidated_payload(quantity: float) -> IngestPayload:
    """An IngestPayload carrying a non-finite quantity, validation skipped.

    `model_construct` is how a real bypass would look: a producer that builds
    the model without validation, or a future field whose guard is missed.
    The client's own serializer is the last line of defence.
    """
    event = RegEngineEvent.model_construct(**event_kwargs(quantity=quantity))
    return IngestPayload.model_construct(source="schema-bounds", events=[event])


@pytest.mark.parametrize(
    "quantity",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "-inf"],
)
def test_live_ingest_refuses_to_sign_a_body_with_a_non_finite_quantity(quantity: float) -> None:
    config = SimulationConfig(
        delivery=DeliveryConfig(
            mode=DestinationMode.LIVE,
            api_key="test-key",
            tenant_id="test-tenant",
        )
    )
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(LiveRegEngineClient().ingest(unvalidated_payload(quantity), config))
    # json.dumps' own wording for allow_nan=False; the point is that it is a
    # local ValueError and not an HTTP error from RegEngine.
    assert "Out of range float" in str(excinfo.value)


def test_default_json_dumps_would_have_emitted_invalid_json() -> None:
    """Why allow_nan=False matters: the default produces a bare NaN token."""
    body = build_wire_body(unvalidated_payload(float("nan")))
    permissive = json.dumps(body, separators=(",", ":"), sort_keys=True)
    assert "NaN" in permissive
    with pytest.raises(ValueError):
        json.loads(permissive, parse_constant=_reject_constant)


def _reject_constant(token: str):
    raise ValueError(f"invalid JSON constant: {token}")


def test_a_finite_payload_still_serializes_and_signs() -> None:
    body = build_wire_body(IngestPayload(source="schema-bounds", events=[RegEngineEvent(**event_kwargs())]))
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True, allow_nan=False)
    assert math.isfinite(json.loads(encoded)["events"][0]["quantity"])


# --- the wire shape is unchanged ----------------------------------------------


def test_wire_shape_is_untouched_by_the_quantity_guard() -> None:
    body = build_wire_body(IngestPayload(source="schema-bounds", events=[RegEngineEvent(**event_kwargs())]))
    assert set(body) == {"source", "events"}
    assert set(body["events"][0]) == {
        "cte_type",
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "location_name",
        "location_gln",
        "timestamp",
        "kdes",
    }


def test_top_level_input_lot_code_hoisting_still_happens() -> None:
    """Guards must not disturb the transformation lineage fix."""
    event = RegEngineEvent(
        **event_kwargs(
            cte_type=CTEType.TRANSFORMATION,
            kdes={INPUT_LOT_CODES_FIELD: ["TLC-IN-1", "TLC-IN-2"]},
        )
    )
    body = build_wire_body(IngestPayload(source="schema-bounds", events=[event]))
    assert body["events"][0][INPUT_LOT_CODES_FIELD] == ["TLC-IN-1", "TLC-IN-2"]


# --- batch size: bounded by the producer, not by the model --------------------


def test_batch_size_is_bounded_by_chunking_rather_than_by_the_payload_model() -> None:
    """`IngestPayload` deliberately carries no max_length; see its comment.

    The cap RegEngine enforces is honoured by chunking every bulk send, and by
    `MockRegEngineService.ingest`, which returns RegEngine's own wording. This
    pins the mitigation that actually bounds what leaves the simulator.
    """
    events = [RegEngineEvent(**event_kwargs(traceability_lot_code=f"TLC-BOUNDS-{i:04d}")) for i in range(MAX_BATCH_EVENTS + 7)]
    chunks = chunk_events(events)
    assert [len(chunk) for chunk in chunks] == [MAX_BATCH_EVENTS, 7]
    assert all(len(chunk) <= MAX_BATCH_EVENTS for chunk in chunks)

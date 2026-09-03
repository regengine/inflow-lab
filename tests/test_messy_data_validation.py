"""Messy-data validation coverage.

REPO_PURPOSE lists "simulate errors and recovery scenarios" as a goal: demos
should show RegEngine-compatible validation catching imperfect supplier data,
not just the happy path. These tests pin that the KDE validator flags missing
required KDEs and malformed transformation inputs, so the "handles imperfection"
story stays true as the rules evolve.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.cte_rules import REQUIRED_KDES, validate_event_kdes
from app.schemas.domain import CTEType, RegEngineEvent


def _harvest_event(**kdes: object) -> RegEngineEvent:
    """A harvesting event whose top-level fields are valid; KDEs are caller-supplied.

    For HARVESTING, ``harvest_date`` and ``reference_document`` are required KDEs
    that live in the ``kdes`` dict (the rest are satisfied by top-level fields),
    so omitting them is the realistic "incomplete supplier payload" case.
    """
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="LOT-MESSY-001",
        product_description="Romaine hearts",
        quantity=10.0,
        unit_of_measure="cases",
        location_name="Test Farm",
        timestamp=datetime(2026, 2, 5, 8, 0, tzinfo=UTC),
        kdes=dict(kdes),
    )


def test_missing_required_kdes_are_flagged() -> None:
    warnings = validate_event_kdes(_harvest_event())  # no KDEs supplied
    flagged = {w.field for w in warnings}
    # The required KDEs that aren't covered by top-level fields must be flagged.
    assert "harvest_date" in flagged
    assert "reference_document" in flagged
    assert all(w.message for w in warnings)


def test_complete_event_has_no_missing_required_warnings() -> None:
    event = _harvest_event(harvest_date="2026-02-05", reference_document="BOL-2026-0205-001")
    missing_required = [w for w in validate_event_kdes(event) if w.message.startswith("Missing expected")]
    assert missing_required == []


def test_malformed_transformation_input_lots_are_flagged() -> None:
    event = RegEngineEvent(
        cte_type=CTEType.TRANSFORMATION,
        traceability_lot_code="SALAD-0205-001",
        product_description="Garden salad mix",
        quantity=500.0,
        unit_of_measure="bags",
        location_name="Processing Plant",
        timestamp=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
        # Required transformation KDEs present, but input lot codes are a bare
        # string instead of a non-empty list — a common real-world malformation.
        kdes=dict.fromkeys(REQUIRED_KDES.get(CTEType.TRANSFORMATION, ()), "placeholder")
        | {"input_traceability_lot_codes": "LOT-A,LOT-B"},
    )
    messages = [w.message for w in validate_event_kdes(event)]
    assert any("input_traceability_lot_codes" in m for m in messages)


@pytest.mark.parametrize(
    "quantity",
    [float("nan"), float("inf"), float("-inf"), float("1e400")],
    ids=["nan", "inf", "negative_inf", "overflow_literal"],
)
def test_non_finite_quantity_is_rejected_by_the_model(quantity: float) -> None:
    """#98: the model itself must refuse a non-finite quantity.

    The importer guards its own path, but ``RegEngineEvent`` is also built
    directly by the engine, the demo fixtures, and -- via ``IngestPayload``
    -- by whatever JSON a caller POSTs to the mock ingest endpoint. A
    non-finite float clears every application-level check (``nan <= 0`` is
    False; infinities are genuinely positive) and only fails later, at
    ``json.dumps``, by emitting a bare ``NaN``/``Infinity`` token that is not
    RFC 8259. Rejecting at the model closes the gap for every producer at
    once, and makes the mock ingest route answer with the same whole-batch
    422 that live RegEngine's own Field() constraints produce during body
    parsing.
    """
    with pytest.raises(ValidationError):
        RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code="TLC-NONFINITE-001",
            product_description="Romaine Lettuce",
            quantity=quantity,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=datetime(2026, 2, 5, 8, 0, tzinfo=UTC),
        )

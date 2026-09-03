"""Messy-data validation coverage.

REPO_PURPOSE lists "simulate errors and recovery scenarios" as a goal: demos
should show RegEngine-compatible validation catching imperfect supplier data,
not just the happy path. These tests pin that the KDE validator flags missing
required KDEs and malformed transformation inputs, so the "handles imperfection"
story stays true as the rules evolve.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
        kdes={field: "placeholder" for field in REQUIRED_KDES.get(CTEType.TRANSFORMATION, ())}
        | {"input_traceability_lot_codes": "LOT-A,LOT-B"},
    )
    messages = [w.message for w in validate_event_kdes(event)]
    assert any("input_traceability_lot_codes" in m for m in messages)

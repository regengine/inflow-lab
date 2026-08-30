"""Transformation input-lot linkage is required, not merely recommended.

Regression cover for #189. FDA's Transformation KDE requirements have two
unconditional halves: the new lot's own fields, *and*, for each FTL food used
as an ingredient, that input's traceability lot code, product description, and
the quantity/unit of measure used from that lot. `cte_rules` only required the
first half -- input lot linkage sat in `RECOMMENDED_KDES`, so a transformation
event missing it drew at most a soft "Missing recommended" nudge, and there was
no KDE at all for the per-input quantity.

On the classification: `REQUIRED_KDES` stays pinned verbatim to RegEngine's
`REQUIRED_KDES_BY_CTE`, which is its *ingest-time* schema gate. RegEngine does
require input TLCs -- through its compliance rules engine, where the rule is
seeded at `critical` severity citing 21 CFR 1.1350(a)(4)
(`services/shared/rules/seeds.py`). Promoting them into the pinned table would
have claimed live ingest rejects what it actually accepts, so they live in
`FSMA_REQUIRED_KDES` and are reported at required severity instead. See #209,
which called the earlier attempt "effectively a message-string change" --
hence the real `severity` field rather than different wording.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.cte_rules import (
    FSMA_REQUIRED_KDES,
    RECOMMENDED_KDES,
    REQUIRED_KDES,
    SEVERITY_RECOMMENDED,
    SEVERITY_REQUIRED,
    validate_event_kdes,
)
from app.engine import LegitFlowEngine
from app.epcis_export import render_epcis_document
from app.schemas.domain import CTEType, RegEngineEvent, StoredEventRecord


def _transformation(**kdes) -> RegEngineEvent:
    base = {
        "transformation_date": "2026-02-10",
        "location_name": "Meridian Processing",
        "reference_document": "Batch Record BR-1",
    }
    return RegEngineEvent(
        cte_type=CTEType.TRANSFORMATION,
        traceability_lot_code="TLC-OUT-1",
        product_description="Chopped Romaine",
        quantity=100.0,
        unit_of_measure="cases",
        location_name="Meridian Processing",
        timestamp=datetime(2026, 2, 10, 8, 0, tzinfo=UTC),
        kdes={**base, **kdes},
    )


def _warning_for(event: RegEngineEvent, field: str):
    matches = [w for w in validate_event_kdes(event) if w.field == field]
    return matches[0] if matches else None


def test_missing_input_lot_codes_is_flagged_as_required_not_recommended():
    warning = _warning_for(_transformation(), "input_traceability_lot_codes")

    assert warning is not None, "no warning at all for missing input lot linkage"
    assert warning.severity == SEVERITY_REQUIRED
    assert "recommended" not in warning.message.lower()
    assert warning.citation == "21 CFR 1.1350(a)(4)"


def test_missing_input_products_is_flagged_as_required():
    warning = _warning_for(_transformation(), "input_products")

    assert warning is not None
    assert warning.severity == SEVERITY_REQUIRED


def test_missing_per_input_quantity_is_flagged_as_required():
    # The KDE that did not exist at all before -- neither required nor
    # recommended -- despite being an unconditional part of the rule.
    warning = _warning_for(
        _transformation(
            input_traceability_lot_codes=["TLC-IN-1"],
            input_products=["Whole Romaine"],
        ),
        "input_quantities",
    )

    assert warning is not None
    assert warning.severity == SEVERITY_REQUIRED
    assert warning.citation == "21 CFR 1.1350(a)(6)"


def test_a_complete_transformation_draws_no_input_warnings():
    event = _transformation(
        input_traceability_lot_codes=["TLC-IN-1", "TLC-IN-2"],
        input_products=["Whole Romaine", "Whole Spinach"],
        input_quantities=[
            {"lot_code": "TLC-IN-1", "quantity": 60.0, "unit_of_measure": "cases"},
            {"lot_code": "TLC-IN-2", "quantity": 45.0, "unit_of_measure": "cases"},
        ],
    )

    input_fields = {"input_traceability_lot_codes", "input_products", "input_quantities"}
    assert [w for w in validate_event_kdes(event) if w.field in input_fields] == []


def test_an_aggregate_quantity_does_not_satisfy_the_per_input_requirement():
    # A single total, or a yield ratio, is exactly what the engine used to
    # record. The rule asks for the quantity used *from each* lot.
    warning = _warning_for(
        _transformation(
            input_traceability_lot_codes=["TLC-IN-1"],
            input_products=["Whole Romaine"],
            input_quantities=[{"lot_code": "TLC-IN-1", "quantity": 60.0}],
        ),
        "input_quantities",
    )

    assert warning is not None
    assert warning.severity == SEVERITY_REQUIRED


def test_required_kdes_table_is_untouched():
    # The pin to RegEngine's ingest contract must not have moved.
    assert "input_traceability_lot_codes" not in REQUIRED_KDES[CTEType.TRANSFORMATION]
    assert "input_products" not in REQUIRED_KDES[CTEType.TRANSFORMATION]


def test_the_promoted_fields_left_the_recommended_tier():
    recommended = RECOMMENDED_KDES[CTEType.TRANSFORMATION]

    assert "input_traceability_lot_codes" not in recommended
    assert "input_products" not in recommended
    assert {field for field, _ in FSMA_REQUIRED_KDES[CTEType.TRANSFORMATION]} == {
        "input_traceability_lot_codes",
        "input_products",
        "input_quantities",
    }


def test_recommended_fields_still_report_at_recommended_severity():
    warning = _warning_for(_transformation(reference_document_type=None), "reference_document_type")

    assert warning is not None
    assert warning.severity == SEVERITY_RECOMMENDED


def test_the_engine_populates_a_quantity_for_every_input_lot():
    engine = LegitFlowEngine(seed=204)
    transformations = []
    for _ in range(400):
        event, _parents = engine.next_event()
        if event.cte_type == CTEType.TRANSFORMATION:
            transformations.append(event)

    assert transformations, "no transformation events generated"
    for event in transformations:
        lots = event.kdes["input_traceability_lot_codes"]
        quantities = event.kdes["input_quantities"]
        assert [entry["lot_code"] for entry in quantities] == lots
        assert all(entry["quantity"] > 0 for entry in quantities)
        assert all(entry["unit_of_measure"] for entry in quantities)
        assert [w for w in validate_event_kdes(event) if w.field == "input_quantities"] == []


def test_epcis_input_elements_now_carry_a_quantity():
    # The downstream half: every EPCIS inputQuantityList element used to render
    # with no quantity, because nothing upstream captured it.
    record = StoredEventRecord(
        payload_source="test",
        event=_transformation(
            input_traceability_lot_codes=["TLC-IN-1"],
            input_products=["Whole Romaine"],
            input_quantities=[
                {"lot_code": "TLC-IN-1", "quantity": 60.0, "unit_of_measure": "cases"}
            ],
        ),
        parent_lot_codes=["TLC-IN-1"],
    )

    document = render_epcis_document([record], source="test", location_gln=lambda name: "")
    inputs = document["epcisBody"]["eventList"][0]["inputQuantityList"]

    assert inputs[0]["quantity"] == 60.0
    assert inputs[0]["uom"] == "cases"


def test_epcis_input_elements_omit_quantity_when_it_was_never_recorded():
    # Falling back to the old shape is correct; guessing a quantity would be
    # worse than the gap, and validate_event_kdes is what reports it.
    record = StoredEventRecord(
        payload_source="test",
        event=_transformation(input_traceability_lot_codes=["TLC-IN-1"]),
        parent_lot_codes=["TLC-IN-1"],
    )

    document = render_epcis_document([record], source="test", location_gln=lambda name: "")
    inputs = document["epcisBody"]["eventList"][0]["inputQuantityList"]

    assert "quantity" not in inputs[0]

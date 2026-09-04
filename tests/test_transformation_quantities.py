"""Per-input-lot quantities on transformation CTEs (#159, and #189's engine half).

FSMA 204 §1.1350(c) asks, for every FTL food used as an ingredient, for the
quantity and unit of measure *used from that lot*. Nothing upstream of the
exporter recorded it, so every EPCIS ``inputQuantityList`` entry rendered
without a ``quantity`` key even though EPCIS ``QuantityElement`` requires one
alongside ``epcClass``.

``transformation_kdes()`` now emits ``input_quantities`` in exactly the shape
``app.epcis_export._input_lot_details`` already reads, populated from the real
input ``Lot`` objects so the numbers are true to the simulated mass balance.

The classification half of #189 (promoting the input-lot KDEs from recommended
to required) is deliberately NOT done here — see
``test_required_kdes_still_match_the_pinned_regengine_contract`` below.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cte_rules import REQUIRED_KDES
from app.engine import LegitFlowEngine
from app.epcis_export import render_epcis_document
from app.industry_adapters import get_industry_adapter
from app.schemas.domain import CTEType, StoredEventRecord

INDUSTRIES = ("produce", "seafood", "dairy")
SCENARIOS = ("leafy_greens_supplier", "seafood_first_receiver", "dairy_continuous_flow")


def _gln(location_name: str) -> str:
    return f"09521234{abs(hash(location_name)) % 100000:05d}"


def _transformation_runs(
    *,
    seed: int = 204,
    scenario: str = "leafy_greens_supplier",
    steps: int = 400,
) -> tuple[list[StoredEventRecord], LegitFlowEngine]:
    """Drive a seeded engine and wrap its output in store-shaped records."""
    engine = LegitFlowEngine(seed=seed, scenario=scenario)
    records: list[StoredEventRecord] = []
    for sequence_no in range(1, steps + 1):
        event, parents = engine.next_event()
        records.append(
            StoredEventRecord(
                sequence_no=sequence_no,
                payload_source="transformation-quantity-test",
                event=event,
                parent_lot_codes=list(parents),
            )
        )
    return records, engine


def _transformation_events(records: list[StoredEventRecord]) -> list[StoredEventRecord]:
    return [r for r in records if r.event.cte_type == CTEType.TRANSFORMATION]


# --- the KDE itself -------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_transformation_kdes_emit_one_quantity_entry_per_input_lot(scenario: str) -> None:
    records, _ = _transformation_runs(scenario=scenario)
    transformations = _transformation_events(records)
    assert transformations, "seeded run produced no transformation CTEs"

    for record in transformations:
        kdes = record.event.kdes
        entries = kdes["input_quantities"]
        assert isinstance(entries, list) and entries

        # Exactly the exporter's expected shape: a list of dicts keyed by
        # lot_code / quantity / unit_of_measure.
        for entry in entries:
            assert set(entry) == {"lot_code", "quantity", "unit_of_measure"}
            assert isinstance(entry["lot_code"], str) and entry["lot_code"]
            assert isinstance(entry["quantity"], (int, float))
            assert not isinstance(entry["quantity"], bool)
            assert entry["quantity"] > 0
            assert isinstance(entry["unit_of_measure"], str) and entry["unit_of_measure"]

        # One entry per declared input lot, in the same order.
        assert [entry["lot_code"] for entry in entries] == kdes["input_traceability_lot_codes"]
        assert len(entries) == kdes["commingled_input_lot_count"]


def test_input_quantities_are_true_to_the_simulated_mass_balance() -> None:
    """The recorded per-input quantities must reconcile with `yield_ratio`.

    `_transform` consumes each sampled input lot in full, so the summed
    per-input quantities are the batch's `total_input_qty`, and dividing the
    batch's declared output mass by that sum has to reproduce the KDE's own
    `yield_ratio` (which the adapter computes independently).
    """
    records, _ = _transformation_runs()
    transformations = _transformation_events(records)
    assert transformations

    # Group sibling CTEs (one per output lot, plus one per rework lot) back
    # into their batch, so the output side can be summed honestly.
    batches: dict[str, list[StoredEventRecord]] = {}
    for record in transformations:
        batches.setdefault(record.event.kdes["batch_number"], []).append(record)

    checked = 0
    for siblings in batches.values():
        kdes = siblings[0].event.kdes
        total_input = sum(entry["quantity"] for entry in kdes["input_quantities"])
        assert total_input > 0

        declared_outputs = kdes["output_traceability_lot_codes"]
        # Only reconcile batches whose every declared output lot emitted a CTE
        # here (a rework lot can be re-consumed later, but its CTE is emitted
        # with the batch, so in practice this holds for whole batches).
        emitted = {record.event.traceability_lot_code: record.event.quantity for record in siblings}
        if set(declared_outputs) != set(emitted):
            continue

        total_output = sum(emitted.values())
        assert round(total_output / total_input, 3) == pytest.approx(kdes["yield_ratio"], abs=0.002)
        # Yield is a loss, never a gain: the mass balance must not invent food.
        assert total_output <= total_input
        checked += 1

    assert checked, "no complete transformation batch to reconcile"


def test_every_input_quantity_lot_code_is_a_real_upstream_lot() -> None:
    """The quantities are lifted off real input Lots, not invented."""
    records, _ = _transformation_runs()
    known_quantities: dict[str, float] = {}
    checked = 0

    for record in records:
        event = record.event
        if event.cte_type == CTEType.TRANSFORMATION:
            for entry in event.kdes["input_quantities"]:
                lot_code = entry["lot_code"]
                if lot_code in known_quantities:
                    assert entry["quantity"] == pytest.approx(known_quantities[lot_code])
                    checked += 1
        known_quantities.setdefault(event.traceability_lot_code, event.quantity)

    assert checked, "no transformation input lot was traceable to its own CTE"


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_every_adapter_emits_input_quantities(industry: str) -> None:
    """Subclass adapters extend the base dict, so none may drop the new key."""
    adapter = get_industry_adapter(industry)

    class _Lot:
        def __init__(self, code: str, qty: float, uom: str) -> None:
            self.lot_code = code
            self.quantity = qty
            self.unit_of_measure = uom
            self.product_description = f"Product {code}"
            self.tlc_source_reference = f"SRC-{code}"
            self.current_reference_type = "Batch Record"
            self.current_reference_number = "BATCH-1"

    class _Engine:
        def _reference_document(self, ref_type: Any, ref_number: Any) -> str:
            return f"{ref_type} {ref_number}"

    class _Named:
        name = "Processor One"

    from datetime import UTC, datetime

    inputs = [_Lot("TLC-IN-1", 40.0, "cases"), _Lot("TLC-IN-2", 60.0, "cases")]
    kdes = adapter.transformation_kdes(
        engine=_Engine(),
        inputs=inputs,
        outputs=[_Lot("TLC-OUT-1", 80.0, "cases")],
        rework_lots=[],
        processor=_Named(),
        timestamp=datetime(2026, 2, 5, 12, 0, tzinfo=UTC),
        reference_type="Batch Record",
        reference_number="BATCH-1",
        total_input_qty=100.0,
        total_output_qty=80.0,
    )

    assert kdes["input_quantities"] == [
        {"lot_code": "TLC-IN-1", "quantity": 40.0, "unit_of_measure": "cases"},
        {"lot_code": "TLC-IN-2", "quantity": 60.0, "unit_of_measure": "cases"},
    ]


def test_seeded_runs_stay_deterministic() -> None:
    """The new KDE must not consume RNG draws."""
    first, _ = _transformation_runs()
    second, _ = _transformation_runs()

    assert [r.event.cte_type for r in first] == [r.event.cte_type for r in second]
    assert [r.event.quantity for r in first] == [r.event.quantity for r in second]
    assert [
        [entry["quantity"] for entry in r.event.kdes["input_quantities"]]
        for r in _transformation_events(first)
    ] == [
        [entry["quantity"] for entry in r.event.kdes["input_quantities"]]
        for r in _transformation_events(second)
    ]
    assert _transformation_events(first), "sanity: run must include transformations"


# --- #159: the EPCIS export end-to-end ------------------------------------


def test_epcis_input_quantity_list_entries_carry_quantities() -> None:
    records, _ = _transformation_runs()
    document = render_epcis_document(records, source="kde-test", location_gln=_gln)
    events = document["epcisBody"]["eventList"]

    transformation_events = [e for e in events if e["type"] == "TransformationEvent"]
    assert transformation_events, "export produced no TransformationEvent"

    for event in transformation_events:
        input_list = event["inputQuantityList"]
        assert input_list
        for element in input_list:
            # #159's acceptance criterion: a numeric quantity on every entry.
            assert "quantity" in element, element
            assert isinstance(element["quantity"], (int, float))
            assert not isinstance(element["quantity"], bool)
            assert element["quantity"] > 0
            assert element["uom"]


def test_epcis_input_quantities_match_the_source_lots_per_lot() -> None:
    """Quantity correctness per input lot, not merely quantity presence."""
    records, _ = _transformation_runs()
    by_record_id = {str(record.record_id): record for record in records}

    document = render_epcis_document(records, source="kde-test", location_gln=_gln)
    checked = 0

    for event in document["epcisBody"]["eventList"]:
        if event["type"] != "TransformationEvent":
            continue
        record = by_record_id[event["eventID"].removeprefix("urn:uuid:")]
        expected = {
            entry["lot_code"]: (entry["quantity"], entry["unit_of_measure"])
            for entry in record.event.kdes["input_quantities"]
        }
        for element in event["inputQuantityList"]:
            lot_code = element["regengine:traceabilityLotCode"]
            assert lot_code in expected
            quantity, uom = expected[lot_code]
            assert element["quantity"] == pytest.approx(quantity)
            assert element["uom"] == uom
            checked += 1

    assert checked, "no input quantity element to verify"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_epcis_input_quantities_land_for_every_scenario(scenario: str) -> None:
    records, _ = _transformation_runs(scenario=scenario)
    document = render_epcis_document(records, source="kde-test", location_gln=_gln)

    transformation_events = [
        e for e in document["epcisBody"]["eventList"] if e["type"] == "TransformationEvent"
    ]
    assert transformation_events
    assert all(
        "quantity" in element
        for event in transformation_events
        for element in event["inputQuantityList"]
    )


# --- #189: the classification half stays blocked --------------------------


def test_required_kdes_still_match_the_pinned_regengine_contract() -> None:
    """#189 asks for input-lot KDEs to become REQUIRED. That half cannot land here.

    `tests/test_regengine_contract_pin.py` pins `REQUIRED_KDES` verbatim to
    RegEngine's `REQUIRED_KDES_BY_CTE`, whose TRANSFORMATION entry lists no
    input-lot KDE at all. Promoting them unilaterally would make this mock
    validate differently from live RegEngine — the exact drift that pin exists
    to catch. The reclassification belongs to a RegEngine-side change; this
    test is the tripwire that fails if someone does it here anyway.
    """
    transformation_required = REQUIRED_KDES[CTEType.TRANSFORMATION]
    assert "input_traceability_lot_codes" not in transformation_required
    assert "input_products" not in transformation_required
    assert "input_quantities" not in transformation_required

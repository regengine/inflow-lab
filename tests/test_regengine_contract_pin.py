"""Pin app.cte_rules.REQUIRED_KDES to RegEngine's live webhook contract.

The expected table below is copied verbatim from RegEngine
services/ingestion/app/webhook_models.py REQUIRED_KDES_BY_CTE (the source
of truth for live ingest validation). If RegEngine changes its required
KDEs, update BOTH this table and app/cte_rules.py — a mismatch here means
the mock validates differently from live RegEngine, which resurrects the
"green demo, failing live post" drift this pin exists to catch.
"""

from __future__ import annotations

from app.cte_rules import REQUIRED_KDES
from app.schemas.domain import CTEType

REGENGINE_REQUIRED_KDES_BY_CTE: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "harvest_date",
        "location_name",
        "immediate_subsequent_recipient",  # §1.1325(a)(1); contract v3 / A-2190
        "reference_document",
    ),
    CTEType.COOLING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "cooling_date",
        "location_name",
        "immediate_subsequent_recipient",  # §1.1325(b)(1); contract v3 / A-2190
        "harvest_location_name",  # farm where harvested; contract v3 / A-2190
        "reference_document",
    ),
    CTEType.INITIAL_PACKING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "packing_date",
        "location_name",
        "reference_document",
        "harvester_business_name",
        "harvester_business_phone",
        "harvest_date",  # §1.1330(a); contract v3 / A-2190
        "harvest_location_name",  # farm where harvested; contract v3 / A-2190
        "received_date",  # date food received; contract v3 / A-2190
        "received_quantity",  # qty received (distinct from packed); contract v3 / A-2190
    ),
    CTEType.FIRST_LAND_BASED_RECEIVING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "landing_date",
        "receiving_location",
        "reference_document",
    ),
    CTEType.SHIPPING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "ship_date",
        "ship_from_location",
        "ship_to_location",
        "reference_document",
        "tlc_source_reference",
    ),
    CTEType.RECEIVING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "receive_date",
        "receiving_location",
        "immediate_previous_source",
        "reference_document",
        "tlc_source_reference",
    ),
    CTEType.TRANSFORMATION: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "transformation_date",
        "location_name",
        "reference_document",
        "input_lot_codes",  # §1.1350(a)(1); contract v3 / A-2190
        "input_quantities",  # §1.1350(a)(1)(iii); contract v3 / A-2190
        "input_units_of_measure",  # §1.1350(a)(1)(iii); contract v3 / A-2190
        "input_product_descriptions",  # §1.1350(a)(1); contract v3 / A-2190
    ),
}


def test_required_kdes_match_regengine_contract_exactly() -> None:
    assert REQUIRED_KDES == REGENGINE_REQUIRED_KDES_BY_CTE


def test_every_cte_type_has_required_kdes() -> None:
    assert set(REQUIRED_KDES) == set(CTEType)

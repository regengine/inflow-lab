"""Pin app.cte_rules.REQUIRED_KDES to RegEngine's live webhook contract.

The expected table below is copied verbatim from RegEngine
services/ingestion/app/webhook_models.py REQUIRED_KDES_BY_CTE (the source
of truth for live ingest validation). If RegEngine changes its required
KDEs, update BOTH this table and app/cte_rules.py — a mismatch here means
the mock validates differently from live RegEngine, which resurrects the
"green demo, failing live post" drift this pin exists to catch.

NOTE (§1.1330(b)(7)): The TRANSFORMATION entry below already includes
input_traceability_lot_codes and input_products as required, ahead of the
live RegEngine contract. Update the live contract and remove this note once
RegEngine's webhook_models.py is aligned.
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
        "reference_document",
    ),
    CTEType.COOLING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "cooling_date",
        "location_name",
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
        "input_traceability_lot_codes",
        "input_products",
    ),
}


def test_required_kdes_match_regengine_contract_exactly() -> None:
    assert REQUIRED_KDES == REGENGINE_REQUIRED_KDES_BY_CTE


def test_every_cte_type_has_required_kdes() -> None:
    assert set(REQUIRED_KDES) == set(CTEType)

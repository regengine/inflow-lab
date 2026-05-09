from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas.domain import CTEType, RegEngineEvent


@dataclass(frozen=True, slots=True)
class CTEValidationWarning:
    field: str
    message: str


# Mirrors RegEngine's REQUIRED_KDES_BY_CTE
# (services/ingestion/app/webhook_models.py). Top-level IngestEvent fields
# (location_name, traceability_lot_code, product_description, quantity,
# unit_of_measure) are merged with the kdes dict before checking, matching
# how the RegEngine validator resolves required keys.
REQUIRED_KDES: dict[CTEType, tuple[str, ...]] = {
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
    ),
}

RECOMMENDED_KDES: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: ("field_name", "tlc_source_reference"),
    CTEType.COOLING: ("harvest_location", "tlc_source_reference"),
    CTEType.INITIAL_PACKING: ("tlc_source_reference",),
    CTEType.FIRST_LAND_BASED_RECEIVING: ("tlc_source_reference",),
    CTEType.SHIPPING: ("carrier", "reference_document_type"),
    CTEType.RECEIVING: ("reference_document_type",),
    CTEType.TRANSFORMATION: ("input_traceability_lot_codes", "input_products", "reference_document_type"),
}


def validate_event_kdes(event: RegEngineEvent) -> list[CTEValidationWarning]:
    warnings: list[CTEValidationWarning] = []

    # Match RegEngine's validator: merge top-level IngestEvent fields with
    # event.kdes so a required key satisfied at top level isn't flagged.
    available: dict[str, Any] = {
        "location_name": event.location_name,
        "traceability_lot_code": event.traceability_lot_code,
        "product_description": event.product_description,
        "quantity": event.quantity,
        "unit_of_measure": event.unit_of_measure,
        **event.kdes,
    }

    required = REQUIRED_KDES.get(event.cte_type, ())
    for field in required:
        if not _has_value(available.get(field)):
            warnings.append(
                CTEValidationWarning(
                    field=field,
                    message=f"Missing expected {event.cte_type.value} KDE: {field}",
                )
            )

    for field in RECOMMENDED_KDES.get(event.cte_type, ()):
        if not _has_value(available.get(field)):
            warnings.append(
                CTEValidationWarning(
                    field=field,
                    message=f"Missing recommended {event.cte_type.value} KDE: {field}",
                )
            )

    if event.cte_type == CTEType.TRANSFORMATION:
        input_lots = event.kdes.get("input_traceability_lot_codes")
        if _has_value(input_lots) and not _is_nonempty_string_list(input_lots):
            warnings.append(
                CTEValidationWarning(
                    field="input_traceability_lot_codes",
                    message="Transformation input_traceability_lot_codes should be a non-empty list of lot codes",
                )
            )

    return warnings


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def _is_nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.strip() for item in value
    )

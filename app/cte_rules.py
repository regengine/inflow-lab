from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import CTEType, RegEngineEvent


@dataclass(frozen=True, slots=True)
class CTEValidationWarning:
    field: str
    message: str


REQUIRED_KDES: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: (
        "harvest_date",
        "farm_location",
        "reference_document_number",
    ),
    CTEType.COOLING: (
        "cooling_date",
        "cooling_location",
        "reference_document_number",
    ),
    CTEType.INITIAL_PACKING: (
        "pack_date",
        "packing_location",
        "source_traceability_lot_code",
        "reference_document_number",
    ),
    CTEType.SHIPPING: (
        "ship_date",
        "ship_from_location",
        "ship_to_location",
        "reference_document_number",
    ),
    CTEType.RECEIVING: (
        "receive_date",
        "receiving_location",
        "ship_from_location",
        "reference_document_number",
    ),
    CTEType.TRANSFORMATION: (
        "transformation_date",
        "transformation_location",
        "input_traceability_lot_codes",
        "reference_document_number",
    ),
}

RECOMMENDED_KDES: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: ("field_name", "traceability_lot_code_source_reference"),
    CTEType.COOLING: ("harvest_location", "traceability_lot_code_source_reference"),
    CTEType.INITIAL_PACKING: ("farm_location", "traceability_lot_code_source_reference"),
    CTEType.SHIPPING: ("carrier", "reference_document_type"),
    CTEType.RECEIVING: ("reference_document_type", "traceability_lot_code_source_reference"),
    CTEType.TRANSFORMATION: ("input_products", "reference_document_type"),
}


def validate_event_kdes(event: RegEngineEvent) -> list[CTEValidationWarning]:
    warnings: list[CTEValidationWarning] = []
    for field in REQUIRED_KDES[event.cte_type]:
        if not _has_value(event.kdes.get(field)):
            warnings.append(
                CTEValidationWarning(
                    field=field,
                    message=f"Missing expected {event.cte_type.value} KDE: {field}",
                )
            )

    for field in RECOMMENDED_KDES[event.cte_type]:
        if not _has_value(event.kdes.get(field)):
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

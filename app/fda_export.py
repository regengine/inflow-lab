from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Callable, Iterable

from .cte_rules import merged_event_values
from .schemas.domain import CTEType, FDAExportPreset, RegEngineEvent, StoredEventRecord


# The first eleven columns mirror RegEngine's documented FDA request export
# shape and must keep their order. The trailing columns are additive and carry
# the FSMA 204 KDEs the eleven-column shape has no home for: the second
# location description Shipping/Receiving records each require, the
# traceability lot code source reference, and the event's CTE.
FDA_EXPORT_COLUMNS = [
    "Traceability Lot Code",
    "Traceability Lot Code Description",
    "Product Description",
    "Quantity",
    "Unit of Measure",
    "Location Description",
    "Location Identifier (GLN)",
    "Date",
    "Time",
    "Reference Document Type",
    "Reference Document Number",
    "Immediate Subsequent Recipient Location",
    "Immediate Previous Source Location",
    "Traceability Lot Code Source Reference",
    "Event Type (CTE)",
]

# Per-CTE primary "Location Description" KDE. Falls back to
# ``event.location_name`` when the KDE is absent.
_PRIMARY_LOCATION_KDES: dict[CTEType, tuple[str, ...]] = {
    CTEType.SHIPPING: ("ship_from_location",),
    CTEType.RECEIVING: ("receiving_location",),
    CTEType.FIRST_LAND_BASED_RECEIVING: ("receiving_location",),
    CTEType.TRANSFORMATION: ("transformation_location", "location_name"),
}

_SUBSEQUENT_RECIPIENT_KDES = ("ship_to_location", "immediate_subsequent_recipient")
_PREVIOUS_SOURCE_KDES = ("immediate_previous_source", "ship_from_location", "vessel_name")
_TLC_SOURCE_KDES = ("tlc_source_reference", "traceability_lot_code_source_reference")
# What the lot *is*, not what happened to it. Every row used to carry the CTE
# under "Traceability Lot Code Description" (#94), so a human reading the
# exported sheet saw "shipping" where a food description belongs. The CTE now
# has its own column and this one describes the lot, falling back to the food
# description when no dedicated KDE was supplied.
_TLC_DESCRIPTION_KDES = (
    "traceability_lot_code_description",
    "lot_description",
    "tlc_description",
)


def _first_text(values: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if value is not None and not isinstance(value, (str, list, dict)):
            return str(value)
    return ""


def _primary_location(event: RegEngineEvent, values: dict[str, Any]) -> str:
    return (
        _first_text(values, _PRIMARY_LOCATION_KDES.get(event.cte_type, ()))
        or event.location_name
    )


@dataclass(frozen=True, slots=True)
class FDAExportPresetDefinition:
    id: FDAExportPreset
    label: str
    description: str
    requires_lot_code: bool = False
    cte_types: frozenset[CTEType] | None = None


FDA_EXPORT_PRESETS = {
    FDAExportPreset.ALL_RECORDS: FDAExportPresetDefinition(
        id=FDAExportPreset.ALL_RECORDS,
        label="All records",
        description="Full FDA-request export for the selected date range.",
    ),
    FDAExportPreset.LOT_TRACE: FDAExportPresetDefinition(
        id=FDAExportPreset.LOT_TRACE,
        label="Lot trace",
        description="Forward and backward lineage for one Traceability Lot Code.",
        requires_lot_code=True,
    ),
    FDAExportPreset.SHIPMENT_HANDOFF: FDAExportPresetDefinition(
        id=FDAExportPreset.SHIPMENT_HANDOFF,
        label="Shipment handoff",
        description="Shipping and receiving records with reference documents.",
        cte_types=frozenset(
            {
                CTEType.SHIPPING,
                CTEType.RECEIVING,
                CTEType.FIRST_LAND_BASED_RECEIVING,
            }
        ),
    ),
    FDAExportPreset.RECEIVING_LOG: FDAExportPresetDefinition(
        id=FDAExportPreset.RECEIVING_LOG,
        label="Receiving log",
        description="Receiving records for destination-focused FDA requests.",
        cte_types=frozenset(
            {CTEType.RECEIVING, CTEType.FIRST_LAND_BASED_RECEIVING}
        ),
    ),
    FDAExportPreset.TRANSFORMATION_BATCHES: FDAExportPresetDefinition(
        id=FDAExportPreset.TRANSFORMATION_BATCHES,
        label="Transformation batches",
        description="Transformation events for batch and input-lot review.",
        cte_types=frozenset({CTEType.TRANSFORMATION}),
    ),
}


def list_fda_export_preset_summaries() -> list[dict[str, object]]:
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
            "requires_lot_code": preset.requires_lot_code,
        }
        for preset in FDA_EXPORT_PRESETS.values()
    ]


def apply_fda_export_preset(
    records: Iterable[StoredEventRecord],
    preset_id: FDAExportPreset,
) -> list[StoredEventRecord]:
    definition = FDA_EXPORT_PRESETS[preset_id]
    filtered = list(records)
    if definition.cte_types is not None:
        filtered = [record for record in filtered if record.event.cte_type in definition.cte_types]
    return sorted(filtered, key=lambda record: record.event.timestamp)


def render_fda_request_csv(
    records: Iterable[StoredEventRecord],
    location_gln: Callable[[str], str],
) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FDA_EXPORT_COLUMNS)
    writer.writeheader()
    for record in records:
        event = record.event
        values = merged_event_values(event)
        # Normalize to UTC so two events at different absolute instants never
        # render the same Date/Time pair (the export carries no offset column).
        timestamp = event.timestamp
        timestamp = (
            timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
        )
        previous_source = ""
        if event.cte_type in (CTEType.RECEIVING, CTEType.FIRST_LAND_BASED_RECEIVING):
            previous_source = _first_text(values, _PREVIOUS_SOURCE_KDES)
        writer.writerow(
            {
                "Traceability Lot Code": event.traceability_lot_code,
                "Traceability Lot Code Description": (
                    _first_text(values, _TLC_DESCRIPTION_KDES) or event.product_description
                ),
                "Product Description": event.product_description,
                "Quantity": event.quantity,
                "Unit of Measure": event.unit_of_measure,
                "Location Description": _primary_location(event, values),
                "Location Identifier (GLN)": location_gln(event.location_name),
                "Date": timestamp.date().isoformat(),
                "Time": timestamp.time().isoformat(timespec="seconds"),
                "Reference Document Type": event.kdes.get("reference_document_type", ""),
                "Reference Document Number": event.kdes.get("reference_document_number", ""),
                "Immediate Subsequent Recipient Location": _first_text(
                    values, _SUBSEQUENT_RECIPIENT_KDES
                ),
                "Immediate Previous Source Location": previous_source,
                "Traceability Lot Code Source Reference": _first_text(values, _TLC_SOURCE_KDES),
                "Event Type (CTE)": event.cte_type.value,
            }
        )
    return output.getvalue()


def export_filename(preset_id: FDAExportPreset) -> str:
    return f"fda_request_{preset_id.value}.csv"

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import CTEType, FDAExportPreset, StoredEventRecord


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
]


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
        cte_types=frozenset({CTEType.SHIPPING, CTEType.RECEIVING}),
    ),
    FDAExportPreset.RECEIVING_LOG: FDAExportPresetDefinition(
        id=FDAExportPreset.RECEIVING_LOG,
        label="Receiving log",
        description="Receiving records for destination-focused FDA requests.",
        cte_types=frozenset({CTEType.RECEIVING}),
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
        writer.writerow(
            {
                "Traceability Lot Code": event.traceability_lot_code,
                "Traceability Lot Code Description": event.cte_type.value,
                "Product Description": event.product_description,
                "Quantity": event.quantity,
                "Unit of Measure": event.unit_of_measure,
                "Location Description": event.location_name,
                "Location Identifier (GLN)": location_gln(event.location_name),
                "Date": event.timestamp.date().isoformat(),
                "Time": event.timestamp.time().isoformat(timespec="seconds"),
                "Reference Document Type": event.kdes.get("reference_document_type", ""),
                "Reference Document Number": event.kdes.get("reference_document_number", ""),
            }
        )
    return output.getvalue()


def export_filename(preset_id: FDAExportPreset) -> str:
    return f"fda_request_{preset_id.value}.csv"

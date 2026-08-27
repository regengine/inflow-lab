from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterable

from .schemas.domain import CTEType, FDAExportPreset, RegEngineEvent, StoredEventRecord


FDA_EXPORT_COLUMNS = [
    "Traceability Lot Code",
    "Traceability Lot Code Description",
    "Product Description",
    "Quantity",
    "Unit of Measure",
    "Location Description",
    "Location Identifier (GLN)",
    # FDA's CTE/KDE reference requires two location descriptions per
    # Shipping/Receiving record (the counterparty on the handoff, not just
    # this event's own location) plus the TLC-source reference -- both were
    # already sitting in event.kdes but never read here (issue #186).
    "Ship-To / Previous Source Location Description",
    "TLC Source Reference",
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
        # Normalize before splitting -- see normalize_to_utc below (issue #157).
        normalized_timestamp = normalize_to_utc(event.timestamp)
        writer.writerow(
            {
                "Traceability Lot Code": event.traceability_lot_code,
                "Traceability Lot Code Description": event.cte_type.value,
                "Product Description": event.product_description,
                "Quantity": event.quantity,
                "Unit of Measure": event.unit_of_measure,
                "Location Description": event.location_name,
                "Location Identifier (GLN)": location_gln(event.location_name),
                "Ship-To / Previous Source Location Description": _linked_location_description(event),
                "TLC Source Reference": _tlc_source_reference(event),
                "Date": normalized_timestamp.date().isoformat(),
                "Time": normalized_timestamp.time().isoformat(timespec="seconds"),
                "Reference Document Type": event.kdes.get("reference_document_type", ""),
                "Reference Document Number": event.kdes.get("reference_document_number", ""),
            }
        )
    return output.getvalue()


def normalize_to_utc(value: datetime) -> datetime:
    """Normalize to UTC before Date/Time are ever split off of a timestamp.

    Two events at genuinely different absolute instants must not collapse
    onto identical Date/Time text columns just because they were recorded
    with different UTC offsets (issue #157) -- event.timestamp carries
    whatever tzinfo the source data happened to have (a CSV row's explicit
    "+05:00" is preserved as-is by csv_importer._ensure_timezone, which only
    ever fills in a *missing* tzinfo), and this export previously called
    .date()/.time() straight off of that without ever converting first.
    A naive timestamp (no tzinfo at all) is treated as already being UTC --
    the same assumption _ensure_timezone makes -- via .replace() rather than
    handed to .astimezone(), which would silently reinterpret it using
    *this process's* local timezone instead.

    Compliance-relevant choice, stated explicitly: every exported Date and
    Time is UTC wall-clock, full stop. Unlike the EPCIS export (which
    stamps a per-event eventTimeZoneOffset because eventTime there keeps
    the original offset), this FDA CSV has no offset column at all -- so
    normalizing every row to the same fixed zone is what keeps the
    sortable spreadsheet sortable and comparable, rather than adding a
    column FDA's own export format doesn't define a place for.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _linked_location_description(event: RegEngineEvent) -> str:
    """The second, counterparty location FDA's KDE table requires per row.

    "Location Description" above is always *this* event's own location
    (ship-from for Shipping, receiving for Receiving). A Shipping row also
    needs the immediate subsequent recipient, and a Receiving row also needs
    the immediate previous source -- both already live in event.kdes, so a
    Shipping- or Receiving-only export no longer silently drops half of the
    required location pair just because its paired leg isn't in the batch.
    """
    if event.cte_type == CTEType.SHIPPING:
        return event.kdes.get("ship_to_location") or ""
    if event.cte_type == CTEType.RECEIVING:
        return event.kdes.get("immediate_previous_source") or ""
    return ""


def _tlc_source_reference(event: RegEngineEvent) -> str:
    """Same short/long key aliasing cte_rules.merged_event_values uses.

    The engine always sets both tlc_source_reference and the verbose
    traceability_lot_code_source_reference together, but hand-crafted or
    CSV-imported events aren't guaranteed to -- falling back keeps this
    column consistent with what the validator already treats as satisfied.
    """
    return event.kdes.get("tlc_source_reference") or event.kdes.get("traceability_lot_code_source_reference") or ""


def export_filename(preset_id: FDAExportPreset) -> str:
    return f"fda_request_{preset_id.value}.csv"

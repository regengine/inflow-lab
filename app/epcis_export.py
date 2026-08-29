from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Iterable
from urllib.parse import quote

from .schemas.domain import CTEType, StoredEventRecord


EPCIS_CONTEXT = "https://ref.gs1.org/standards/epcis/2.0.0/epcis-context.jsonld"
REGENGINE_EPCIS_CONTEXT = {"regengine": "https://www.regengine.co/ns/epcis#"}

_BIZ_STEPS = {
    CTEType.HARVESTING: "urn:epcglobal:cbv:bizstep:commissioning",
    CTEType.COOLING: "urn:epcglobal:cbv:bizstep:storing",
    CTEType.INITIAL_PACKING: "urn:epcglobal:cbv:bizstep:packing",
    CTEType.FIRST_LAND_BASED_RECEIVING: "urn:epcglobal:cbv:bizstep:receiving",
    CTEType.SHIPPING: "urn:epcglobal:cbv:bizstep:shipping",
    CTEType.RECEIVING: "urn:epcglobal:cbv:bizstep:receiving",
    # "transforming" isn't one of GS1 CBV's 41 standard bizStep terms (verified
    # against GS1's own epcis-context.jsonld, issue #188) -- using the reserved
    # urn:epcglobal:cbv:bizstep: prefix for a term GS1 never defined would
    # masquerade as standard CBV, so this is minted under our own namespace.
    CTEType.TRANSFORMATION: "urn:regengine:bizstep:transformation",
}

# The only sourceDestinationType issue #187 asks for -- CBV also defines
# owning_party/possessing_party, but this app has no ownership/possession
# model distinct from location, so "location" is the one type it can back
# with real data.
#
# The BARE TOKEN, not the urn:epcglobal:cbv:sdt:location alias. GS1's own
# epcis-context.jsonld declares sourceList/destinationList `type` as
# "@type": "@vocab" over exactly three short names -- owning_party,
# possessing_party, location -- so only the token expands to cbv:SDT-location.
# The URN is a legitimate sameAs alias of that term, but it is not one of the
# declared vocabulary entries, so it fails JSON-LD expansion; and the official
# EPCIS 2.0 JSON Schema's source-dest-type carries a negative lookahead
# ("^(?!(urn:epcglobal:cbv|https?://ns\.gs1\.org/cbv/))") that rejects this
# exact prefix outright. Emitting the URN made both documents schema-invalid,
# which is the opposite of what #187 asked for.
_SDT_LOCATION = "location"

_DISPOSITIONS = {
    CTEType.HARVESTING: "urn:epcglobal:cbv:disp:active",
    CTEType.COOLING: "urn:epcglobal:cbv:disp:active",
    CTEType.INITIAL_PACKING: "urn:epcglobal:cbv:disp:active",
    CTEType.FIRST_LAND_BASED_RECEIVING: "urn:epcglobal:cbv:disp:active",
    CTEType.SHIPPING: "urn:epcglobal:cbv:disp:in_transit",
    CTEType.RECEIVING: "urn:epcglobal:cbv:disp:active",
    CTEType.TRANSFORMATION: "urn:epcglobal:cbv:disp:active",
}

_OBJECT_ACTIONS = {
    CTEType.HARVESTING: "ADD",
    CTEType.COOLING: "OBSERVE",
    CTEType.INITIAL_PACKING: "ADD",
    CTEType.FIRST_LAND_BASED_RECEIVING: "ADD",
    CTEType.SHIPPING: "OBSERVE",
    CTEType.RECEIVING: "OBSERVE",
}


def render_epcis_document(
    records: Iterable[StoredEventRecord],
    source: str,
    location_gln: Callable[[str], str],
    creation_date: datetime | None = None,
) -> dict[str, Any]:
    ordered_records = sorted(records, key=lambda record: (record.event.timestamp, record.sequence_no))
    return {
        "@context": [EPCIS_CONTEXT, REGENGINE_EPCIS_CONTEXT],
        "type": "EPCISDocument",
        "schemaVersion": "2.0",
        "creationDate": _format_datetime(creation_date or datetime.now(UTC)),
        "sender": source,
        "epcisBody": {
            "eventList": [
                _render_event(record, location_gln=location_gln)
                for record in ordered_records
            ]
        },
    }


def epcis_filename() -> str:
    return "epcis_events.jsonld"


def _render_event(
    record: StoredEventRecord,
    location_gln: Callable[[str], str],
) -> dict[str, Any]:
    if record.event.cte_type == CTEType.TRANSFORMATION:
        event = _render_transformation_event(record, location_gln)
    else:
        event = _render_object_event(record, location_gln)

    event["eventID"] = f"urn:uuid:{record.record_id}"
    event["eventTime"] = _format_datetime(record.event.timestamp)
    event["eventTimeZoneOffset"] = _timezone_offset(record.event.timestamp)
    event["bizStep"] = _BIZ_STEPS[record.event.cte_type]
    event["disposition"] = _DISPOSITIONS[record.event.cte_type]
    event["readPoint"] = _location_reference(
        record.event.location_name, location_gln, record.event.location_gln
    )
    event["bizLocation"] = _location_reference(
        record.event.location_name, location_gln, record.event.location_gln
    )

    # readPoint/bizLocation only ever describe *this* event's own location.
    # CBV's mechanism for "who this lot moved from/to" in a handoff is
    # sourceList/destinationList, not a second bizLocation -- without these,
    # the ship-to and previous-source KDEs were only ever visible inside the
    # free-form regengine:kdes extension (issue #187).
    source_list = _source_list(record)
    if source_list:
        event["sourceList"] = source_list
    destination_list = _destination_list(record)
    if destination_list:
        event["destinationList"] = destination_list

    event["regengine:sequenceNo"] = record.sequence_no
    event["regengine:cteType"] = record.event.cte_type.value
    event["regengine:traceabilityLotCode"] = record.event.traceability_lot_code
    event["regengine:productDescription"] = record.event.product_description
    event["regengine:parentLotCodes"] = _input_lot_codes(record)
    event["regengine:kdes"] = record.event.kdes

    transactions = _biz_transactions(record)
    if transactions:
        event["bizTransactionList"] = transactions

    return event


def _render_object_event(
    record: StoredEventRecord,
    location_gln: Callable[[str], str],
) -> dict[str, Any]:
    event = record.event
    return {
        "type": "ObjectEvent",
        "action": _OBJECT_ACTIONS[event.cte_type],
        "quantityList": [
            _quantity_element(
                lot_code=event.traceability_lot_code,
                quantity=event.quantity,
                unit_of_measure=event.unit_of_measure,
                product_description=event.product_description,
            )
        ],
        "regengine:location": _location_reference(
            event.location_name, location_gln, event.location_gln
        ),
    }


def _render_transformation_event(
    record: StoredEventRecord,
    location_gln: Callable[[str], str],
) -> dict[str, Any]:
    event = record.event
    batch_number = _transformation_batch_number(record)
    transformation_id = (
        f"urn:regengine:batch:{quote(batch_number, safe='')}"
        if batch_number
        else f"urn:regengine:transformation:{record.record_id}"
    )
    return {
        "type": "TransformationEvent",
        "transformationID": transformation_id,
        "inputQuantityList": [
            _input_quantity_element(record, lot_code)
            for lot_code in _input_lot_codes(record)
        ],
        "outputQuantityList": [
            _quantity_element(
                lot_code=event.traceability_lot_code,
                quantity=event.quantity,
                unit_of_measure=event.unit_of_measure,
                product_description=event.product_description,
            )
        ],
        "regengine:location": _location_reference(
            event.location_name, location_gln, event.location_gln
        ),
    }


def _transformation_batch_number(record: StoredEventRecord) -> str | None:
    batch_number = record.event.kdes.get("batch_number")
    if isinstance(batch_number, str) and batch_number:
        return batch_number

    reference_type = record.event.kdes.get("reference_document_type")
    reference_number = record.event.kdes.get("reference_document_number")
    if (
        isinstance(reference_type, str)
        and "batch" in reference_type.lower()
        and isinstance(reference_number, str)
        and reference_number
    ):
        return reference_number
    return None


def _quantity_element(
    lot_code: str,
    quantity: float | None = None,
    unit_of_measure: str | None = None,
    product_description: str | None = None,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "epcClass": _lot_identifier(lot_code),
        "regengine:traceabilityLotCode": lot_code,
    }
    if quantity is not None:
        element["quantity"] = quantity
    if unit_of_measure:
        element["uom"] = unit_of_measure
    if product_description:
        element["regengine:productDescription"] = product_description
    return element


def _input_quantity_element(record: StoredEventRecord, lot_code: str) -> dict[str, Any]:
    """quantity/uom for one transformation input lot, read if it was ever recorded.

    EPCIS's QuantityElement schema requires `quantity` alongside `epcClass`
    (issue #159), but as of this fix nothing upstream of this module
    actually captures a *per-input* quantity for transformation events:
    industry_adapters.transformation_kdes (which builds
    input_traceability_lot_codes) only ever computes an aggregate
    yield_ratio across all inputs, so engine.py's per-lot
    Lot.quantity/.unit_of_measure never reaches event.kdes for any
    engine-generated or bundled demo-fixture transformation today --
    verified directly against both files, and independently documented by
    cte_rules.py's TRANSFORMATION_INPUT_LINKAGE_KDES comment (issue #189).
    Fabricating a number here would be worse than omitting it for a
    regulatory export, so this reads an "input_lot_quantities" KDE
    (lot_code -> {"quantity": ..., "unit_of_measure": ...}) if one is
    present -- keyed by lot code rather than positionally paired with
    input_traceability_lot_codes, since _input_lot_codes() above merges
    lot codes from three different sources that don't share one common
    order -- and otherwise leaves quantity/uom out, exactly as before.
    A hand-crafted or CSV-imported event's free-form kdes JSON can already
    populate this key today; making industry_adapters.transformation_kdes
    do the same for engine-generated events is the upstream change that
    would make this non-empty for the simulator's own data (see this
    project's issue #159 for the full writeup of that gap).
    """
    per_lot = record.event.kdes.get("input_lot_quantities")
    quantity: float | None = None
    unit_of_measure: str | None = None
    if isinstance(per_lot, dict):
        entry = per_lot.get(lot_code)
        if isinstance(entry, dict):
            raw_quantity = entry.get("quantity")
            # bool is a subclass of int in Python -- exclude it explicitly
            # so a stray True/False can't be coerced into a fake quantity.
            if isinstance(raw_quantity, (int, float)) and not isinstance(raw_quantity, bool):
                quantity = float(raw_quantity)
            raw_uom = entry.get("unit_of_measure")
            if isinstance(raw_uom, str) and raw_uom:
                unit_of_measure = raw_uom
    return _quantity_element(lot_code=lot_code, quantity=quantity, unit_of_measure=unit_of_measure)


def _input_lot_codes(record: StoredEventRecord) -> list[str]:
    lot_codes: list[str] = []
    for lot_code in record.parent_lot_codes:
        if lot_code not in lot_codes:
            lot_codes.append(lot_code)

    source_lot_code = record.event.kdes.get("source_traceability_lot_code")
    if isinstance(source_lot_code, str) and source_lot_code not in lot_codes:
        lot_codes.append(source_lot_code)

    # The kdes copy first, then the top-level field. Reading only the kdes
    # copy meant an event that carried the value where the contract says to
    # carry it -- top-level -- rendered an empty inputQuantityList, losing the
    # input-to-output lineage link that is the entire point of a
    # transformation CTE.
    input_lot_codes = record.event.kdes.get("input_traceability_lot_codes", [])
    if isinstance(input_lot_codes, list):
        for lot_code in input_lot_codes:
            if isinstance(lot_code, str) and lot_code not in lot_codes:
                lot_codes.append(lot_code)

    for lot_code in record.event.input_traceability_lot_codes or []:
        if isinstance(lot_code, str) and lot_code not in lot_codes:
            lot_codes.append(lot_code)

    return lot_codes


def _source_list(record: StoredEventRecord) -> list[dict[str, str]]:
    """Receiving's immediate previous source, in CBV's sourceList shape."""
    if record.event.cte_type != CTEType.RECEIVING:
        return []
    previous_source = record.event.kdes.get("immediate_previous_source")
    if not isinstance(previous_source, str) or not previous_source:
        return []
    return [{"type": _SDT_LOCATION, "source": _location_id(previous_source)}]


def _destination_list(record: StoredEventRecord) -> list[dict[str, str]]:
    """Shipping's immediate subsequent recipient, in CBV's destinationList shape."""
    if record.event.cte_type != CTEType.SHIPPING:
        return []
    ship_to = record.event.kdes.get("ship_to_location")
    if not isinstance(ship_to, str) or not ship_to:
        return []
    return [{"type": _SDT_LOCATION, "destination": _location_id(ship_to)}]


def _biz_transactions(record: StoredEventRecord) -> list[dict[str, str]]:
    reference_type = record.event.kdes.get("reference_document_type")
    reference_number = record.event.kdes.get("reference_document_number")
    if not isinstance(reference_type, str) or not isinstance(reference_number, str):
        return []
    if not reference_type or not reference_number:
        return []

    return [
        {
            "type": _reference_type_identifier(reference_type),
            "bizTransaction": f"urn:regengine:document:{quote(reference_number, safe='')}",
            "regengine:documentType": reference_type,
            "regengine:documentNumber": reference_number,
        }
    ]


def _lot_identifier(lot_code: str) -> str:
    return f"urn:regengine:lot:{quote(lot_code, safe='')}"


def _location_reference(
    location_name: str,
    location_gln: Callable[[str], str],
    event_gln: str | None = None,
) -> dict[str, str]:
    # Registry lookup first, then the event's own GLN (#162). csv_importer
    # threads a GLN column into RegEngineEvent.location_gln, but this only ever
    # consulted the engine's static name->GLN registry -- which an imported
    # location is not in -- so a supplied GLN never reached the EPCIS document.
    gln = location_gln(location_name) or event_gln or ""
    reference = {
        "id": _location_id(location_name),
        "regengine:locationName": location_name,
    }
    if gln:
        reference["regengine:gln"] = gln
    return reference


def _location_id(location_name: str) -> str:
    return f"urn:regengine:location:{quote(location_name, safe='')}"


def _reference_type_identifier(reference_type: str) -> str:
    normalized = reference_type.strip().lower().replace(" ", "_")
    return f"urn:regengine:document_type:{quote(normalized, safe='')}"


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _timezone_offset(value: datetime) -> str:
    offset = value.utcoffset() if value.tzinfo else None
    if offset is None:
        return "+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"

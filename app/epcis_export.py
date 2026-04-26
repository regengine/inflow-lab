from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Iterable
from urllib.parse import quote

from .models import CTEType, StoredEventRecord


EPCIS_CONTEXT = "https://ref.gs1.org/standards/epcis/2.0.0/epcis-context.jsonld"
REGENGINE_EPCIS_CONTEXT = {"regengine": "https://www.regengine.co/ns/epcis#"}

_BIZ_STEPS = {
    CTEType.HARVESTING: "urn:epcglobal:cbv:bizstep:commissioning",
    CTEType.COOLING: "urn:epcglobal:cbv:bizstep:storing",
    CTEType.INITIAL_PACKING: "urn:epcglobal:cbv:bizstep:packing",
    CTEType.SHIPPING: "urn:epcglobal:cbv:bizstep:shipping",
    CTEType.RECEIVING: "urn:epcglobal:cbv:bizstep:receiving",
    CTEType.TRANSFORMATION: "urn:epcglobal:cbv:bizstep:transforming",
}

_DISPOSITIONS = {
    CTEType.HARVESTING: "urn:epcglobal:cbv:disp:active",
    CTEType.COOLING: "urn:epcglobal:cbv:disp:active",
    CTEType.INITIAL_PACKING: "urn:epcglobal:cbv:disp:active",
    CTEType.SHIPPING: "urn:epcglobal:cbv:disp:in_transit",
    CTEType.RECEIVING: "urn:epcglobal:cbv:disp:active",
    CTEType.TRANSFORMATION: "urn:epcglobal:cbv:disp:active",
}

_OBJECT_ACTIONS = {
    CTEType.HARVESTING: "ADD",
    CTEType.COOLING: "OBSERVE",
    CTEType.INITIAL_PACKING: "ADD",
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
    event["readPoint"] = _location_reference(record.event.location_name, location_gln)
    event["bizLocation"] = _location_reference(record.event.location_name, location_gln)
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
        "regengine:location": _location_reference(event.location_name, location_gln),
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
            _quantity_element(lot_code=lot_code)
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
        "regengine:location": _location_reference(event.location_name, location_gln),
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


def _input_lot_codes(record: StoredEventRecord) -> list[str]:
    lot_codes: list[str] = []
    for lot_code in record.parent_lot_codes:
        if lot_code not in lot_codes:
            lot_codes.append(lot_code)

    source_lot_code = record.event.kdes.get("source_traceability_lot_code")
    if isinstance(source_lot_code, str) and source_lot_code not in lot_codes:
        lot_codes.append(source_lot_code)

    input_lot_codes = record.event.kdes.get("input_traceability_lot_codes", [])
    if isinstance(input_lot_codes, list):
        for lot_code in input_lot_codes:
            if isinstance(lot_code, str) and lot_code not in lot_codes:
                lot_codes.append(lot_code)

    return lot_codes


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


def _location_reference(location_name: str, location_gln: Callable[[str], str]) -> dict[str, str]:
    gln = location_gln(location_name)
    reference = {
        "id": f"urn:regengine:location:{quote(location_name, safe='')}",
        "regengine:locationName": location_name,
    }
    if gln:
        reference["regengine:gln"] = gln
    return reference


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

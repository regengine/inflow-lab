from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .models import CSVImportError, CSVImportType, CTEType, RegEngineEvent


EVENT_REQUIRED_FIELDS = (
    "cte_type",
    "traceability_lot_code",
    "product_description",
    "quantity",
    "unit_of_measure",
    "location_name",
    "timestamp",
)
SEED_REQUIRED_FIELDS = (
    "traceability_lot_code",
    "product_description",
    "quantity",
    "unit_of_measure",
    "location_name",
)
CONTROL_FIELDS = {
    "source",
    "parent_lot_codes",
    "import_type",
}
TOP_LEVEL_EVENT_FIELDS = set(EVENT_REQUIRED_FIELDS)
LIST_KDE_FIELDS = {
    "input_traceability_lot_codes",
    "input_products",
}


@dataclass(slots=True)
class ParsedCSVImport:
    total: int
    events: list[RegEngineEvent]
    parent_lot_codes: list[list[str]]
    errors: list[CSVImportError]


def parse_csv_import(
    import_type: CSVImportType,
    csv_text: str,
    default_timestamp: datetime | None = None,
) -> ParsedCSVImport:
    default_timestamp = _ensure_timezone(default_timestamp or datetime.now(UTC))
    if not csv_text.strip():
        return ParsedCSVImport(
            total=0,
            events=[],
            parent_lot_codes=[],
            errors=[CSVImportError(row=0, field="csv_text", message="CSV content is empty")],
        )

    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), skipinitialspace=True)
    header_errors = _header_errors(reader.fieldnames)
    if header_errors:
        return ParsedCSVImport(total=0, events=[], parent_lot_codes=[], errors=header_errors)

    total = 0
    events: list[RegEngineEvent] = []
    parent_lot_codes: list[list[str]] = []
    errors: list[CSVImportError] = []

    for row_number, raw_row in enumerate(reader, start=2):
        row = _normalize_row(raw_row)
        if _is_blank(row):
            continue
        total += 1
        if import_type == CSVImportType.SCHEDULED_EVENTS:
            event, parents, row_errors = _parse_scheduled_event(row, row_number)
        else:
            event, parents, row_errors = _parse_seed_lot(row, row_number, default_timestamp)

        if row_errors:
            errors.extend(row_errors)
            continue
        assert event is not None
        events.append(event)
        parent_lot_codes.append(parents)

    if total == 0 and not errors:
        errors.append(CSVImportError(row=0, field="csv_text", message="CSV contains no data rows"))

    return ParsedCSVImport(total=total, events=events, parent_lot_codes=parent_lot_codes, errors=errors)


def _parse_scheduled_event(
    row: dict[str, str],
    row_number: int,
) -> tuple[RegEngineEvent | None, list[str], list[CSVImportError]]:
    errors = _missing_required(row, row_number, EVENT_REQUIRED_FIELDS)
    if errors:
        return None, [], errors

    quantity = _parse_quantity(row["quantity"], row_number, errors)
    timestamp = _parse_timestamp(row["timestamp"], row_number, "timestamp", errors)
    cte_type = _parse_cte_type(row["cte_type"], row_number, errors)
    kdes = _parse_kdes(row, row_number, errors)

    if errors:
        return None, [], errors

    return _build_event(
        row=row,
        row_number=row_number,
        cte_type=cte_type,
        quantity=quantity,
        timestamp=timestamp,
        kdes=kdes,
        parent_lot_codes=_derive_parent_lot_codes(row, kdes),
    )


def _parse_seed_lot(
    row: dict[str, str],
    row_number: int,
    default_timestamp: datetime,
) -> tuple[RegEngineEvent | None, list[str], list[CSVImportError]]:
    errors = _missing_required(row, row_number, SEED_REQUIRED_FIELDS)
    if errors:
        return None, [], errors

    quantity = _parse_quantity(row["quantity"], row_number, errors)
    timestamp = (
        _parse_timestamp(row["timestamp"], row_number, "timestamp", errors)
        if row.get("timestamp")
        else default_timestamp
    )
    kdes = _parse_kdes(row, row_number, errors)

    if errors:
        return None, [], errors

    kdes.setdefault("harvest_date", timestamp.date().isoformat())
    kdes.setdefault("farm_location", row["location_name"])
    if row.get("field_name"):
        kdes.setdefault("field_name", row["field_name"])
    if row.get("immediate_subsequent_recipient"):
        kdes.setdefault("immediate_subsequent_recipient", row["immediate_subsequent_recipient"])
    kdes.setdefault("reference_document_type", "Seed Lot Import")
    kdes.setdefault("reference_document_number", f"CSV-{row['traceability_lot_code']}")
    kdes.setdefault("traceability_lot_code_source_reference", f"CSV-SEED-{row['traceability_lot_code']}")

    return _build_event(
        row=row,
        row_number=row_number,
        cte_type=CTEType.HARVESTING,
        quantity=quantity,
        timestamp=timestamp,
        kdes=kdes,
        parent_lot_codes=[],
    )


def _build_event(
    row: dict[str, str],
    row_number: int,
    cte_type: CTEType | None,
    quantity: float | None,
    timestamp: datetime | None,
    kdes: dict[str, Any],
    parent_lot_codes: list[str],
) -> tuple[RegEngineEvent | None, list[str], list[CSVImportError]]:
    errors: list[CSVImportError] = []
    if cte_type is None or quantity is None or timestamp is None:
        return None, [], errors

    try:
        event = RegEngineEvent(
            cte_type=cte_type,
            traceability_lot_code=row["traceability_lot_code"],
            product_description=row["product_description"],
            quantity=quantity,
            unit_of_measure=row["unit_of_measure"],
            location_name=row["location_name"],
            timestamp=timestamp,
            kdes=kdes,
        )
    except ValidationError as exc:
        for error in exc.errors():
            location = error.get("loc", ())
            field = ".".join(str(part) for part in location) if location else None
            errors.append(
                CSVImportError(row=row_number, field=field, message=str(error.get("msg", "Invalid value")))
            )
        return None, [], errors

    return event, parent_lot_codes, []


def _header_errors(fieldnames: list[str] | None) -> list[CSVImportError]:
    if not fieldnames:
        return [CSVImportError(row=1, field="header", message="CSV header row is required")]

    normalized = [_normalize_header(fieldname or "") for fieldname in fieldnames]
    if any(not fieldname for fieldname in normalized):
        return [CSVImportError(row=1, field="header", message="CSV header names cannot be blank")]

    duplicates = sorted({fieldname for fieldname in normalized if normalized.count(fieldname) > 1})
    if duplicates:
        return [
            CSVImportError(
                row=1,
                field="header",
                message=f"Duplicate CSV header after normalization: {', '.join(duplicates)}",
            )
        ]
    return []


def _normalize_row(raw_row: dict[str | None, str | None]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in raw_row.items():
        if key is None:
            continue
        normalized[_normalize_header(key)] = (value or "").strip()
    return normalized


def _normalize_header(header: str) -> str:
    normalized = header.strip().lstrip("\ufeff").lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def _is_blank(row: dict[str, str]) -> bool:
    return all(value == "" for value in row.values())


def _missing_required(
    row: dict[str, str],
    row_number: int,
    required_fields: tuple[str, ...],
) -> list[CSVImportError]:
    return [
        CSVImportError(row=row_number, field=field, message=f"Missing required field: {field}")
        for field in required_fields
        if not row.get(field)
    ]


def _parse_quantity(value: str, row_number: int, errors: list[CSVImportError]) -> float | None:
    try:
        quantity = float(value)
    except ValueError:
        errors.append(CSVImportError(row=row_number, field="quantity", message="Quantity must be numeric"))
        return None

    if quantity <= 0:
        errors.append(CSVImportError(row=row_number, field="quantity", message="Quantity must be greater than 0"))
        return None
    return quantity


def _parse_timestamp(
    value: str,
    row_number: int,
    field: str,
    errors: list[CSVImportError],
) -> datetime | None:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.fromisoformat(f"{value}T00:00:00+00:00")
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(CSVImportError(row=row_number, field=field, message="Timestamp must be ISO 8601"))
        return None
    return _ensure_timezone(parsed)


def _ensure_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_cte_type(value: str, row_number: int, errors: list[CSVImportError]) -> CTEType | None:
    try:
        return CTEType(value)
    except ValueError:
        allowed = ", ".join(cte.value for cte in CTEType)
        errors.append(CSVImportError(row=row_number, field="cte_type", message=f"Unsupported cte_type: {value}. Expected one of: {allowed}"))
        return None


def _parse_kdes(
    row: dict[str, str],
    row_number: int,
    errors: list[CSVImportError],
) -> dict[str, Any]:
    kdes: dict[str, Any] = {}
    raw_kdes = row.get("kdes", "")
    if raw_kdes:
        try:
            parsed = json.loads(raw_kdes)
        except json.JSONDecodeError as exc:
            errors.append(CSVImportError(row=row_number, field="kdes", message=f"KDEs must be a JSON object: {exc.msg}"))
        else:
            if not isinstance(parsed, dict):
                errors.append(CSVImportError(row=row_number, field="kdes", message="KDEs must be a JSON object"))
            else:
                kdes.update(parsed)

    excluded_fields = TOP_LEVEL_EVENT_FIELDS | CONTROL_FIELDS | {"kdes", "timestamp"}
    for field, value in row.items():
        if not value or field in excluded_fields:
            continue
        if field.startswith("kde_"):
            kdes[field.removeprefix("kde_")] = _coerce_kde_value(field.removeprefix("kde_"), value)
        else:
            kdes[field] = _coerce_kde_value(field, value)
    return kdes


def _coerce_kde_value(field: str, value: str) -> Any:
    if field in LIST_KDE_FIELDS:
        return _parse_list(value)
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _derive_parent_lot_codes(row: dict[str, str], kdes: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    if row.get("parent_lot_codes"):
        candidates.extend(_parse_list(row["parent_lot_codes"]))

    source_lot_code = kdes.get("source_traceability_lot_code")
    if isinstance(source_lot_code, str):
        candidates.append(source_lot_code)

    input_lot_codes = kdes.get("input_traceability_lot_codes")
    if isinstance(input_lot_codes, str):
        parsed_input_lot_codes = _parse_list(input_lot_codes)
        kdes["input_traceability_lot_codes"] = parsed_input_lot_codes
        candidates.extend(parsed_input_lot_codes)
    elif isinstance(input_lot_codes, list):
        candidates.extend(str(lot_code) for lot_code in input_lot_codes if str(lot_code).strip())

    return _dedupe(candidates)


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]

    parts = re.split(r"[|;,]", value)
    return [part.strip() for part in parts if part.strip()]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped

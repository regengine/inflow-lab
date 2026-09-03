"""Negative tests for CSV import's validation errors (#129).

app/csv_importer.py is real per-branch validation logic, but until now
tests/test_csv_importer.py only covered the happy path plus one row that
triggers several field errors at once. Seven distinct rejection branches
had no coverage at all: an empty CSV body, a header-only CSV with no data
rows, a missing header row, a blank header name, duplicate normalized
headers, a non-numeric quantity, and a quantity that is zero or negative.

Kept in its own file rather than added to tests/test_csv_importer.py or
tests/test_importer_robustness.py -- neither may be edited (strict
per-file ownership across parallel workstreams).
"""

from __future__ import annotations

import pytest

from app.csv_importer import parse_csv_import
from app.schemas.domain import CSVImportType


SCHEDULED_HEADER = (
    "cte_type,traceability_lot_code,product_description,quantity,"
    "unit_of_measure,location_name,timestamp"
)


def _scheduled_row(lot: str = "TLC-VALID-001", quantity: str = "10") -> str:
    """A row with every EVENT_REQUIRED_FIELDS column filled with a value
    that passes its own check, so the only error a caller can provoke is
    whatever they intentionally broke via `quantity`.
    """
    return f"harvesting,{lot},Romaine Lettuce,{quantity},cases,Valley Fresh Farms,2026-03-01T08:00:00Z"


# ---------------------------------------------------------------------------
# An empty (or whitespace-only) csv_text short-circuits before row parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("csv_text", ["", "   \n\t  "], ids=["empty-string", "whitespace-only"])
def test_empty_csv_text_is_rejected(csv_text: str) -> None:
    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.total == 0
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 0
    assert error.field == "csv_text"
    assert error.message == "CSV content is empty"


# ---------------------------------------------------------------------------
# A valid header with zero data rows is a distinct error from an empty file.
# ---------------------------------------------------------------------------


def test_header_only_csv_with_no_data_rows_is_rejected() -> None:
    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, SCHEDULED_HEADER + "\n")

    assert parsed.total == 0
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 0
    assert error.field == "csv_text"
    assert error.message == "CSV contains no data rows"


# ---------------------------------------------------------------------------
# _header_errors's three distinct branches
# ---------------------------------------------------------------------------


def test_missing_header_row_is_rejected() -> None:
    # A lone byte-order mark. U+FEFF is not whitespace by str.strip()'s own
    # definition, so this survives the empty-body check above as "real"
    # content -- but csv.DictReader sees an empty stream once the BOM is
    # stripped, so fieldnames is None. This is exactly what "Save As
    # UTF-8 CSV" of a completely empty spreadsheet produces on disk: bytes
    # in the file, but no header row at all, which must be reported
    # distinctly from an empty body.
    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, "﻿")

    assert parsed.total == 0
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 1
    assert error.field == "header"
    assert error.message == "CSV header row is required"


def test_blank_header_name_is_rejected() -> None:
    # The header check runs before any row is parsed, so a data row isn't
    # needed to reach it -- header-only content is enough.
    csv_text = "cte_type,,product_description,quantity,unit_of_measure,location_name,timestamp\n"

    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.total == 0
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 1
    assert error.field == "header"
    assert error.message == "CSV header names cannot be blank"


def test_duplicate_normalized_header_is_rejected() -> None:
    # "traceability_lot_code" and "Traceability_Lot_Code" collide once both
    # are lowercased by _normalize_header -- the exact ambiguity this check
    # exists to catch before it can silently overwrite one column's data
    # with the other's during row parsing.
    csv_text = (
        "cte_type,traceability_lot_code,Traceability_Lot_Code,product_description,"
        "quantity,unit_of_measure,location_name,timestamp\n"
    )

    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.total == 0
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 1
    assert error.field == "header"
    assert error.message == "Duplicate CSV header after normalization: traceability_lot_code"


# ---------------------------------------------------------------------------
# _parse_quantity's branches
# ---------------------------------------------------------------------------


def test_non_numeric_quantity_is_rejected() -> None:
    csv_text = SCHEDULED_HEADER + "\n" + _scheduled_row(lot="TLC-QTY-BAD", quantity="abc") + "\n"

    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.total == 1
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 2
    assert error.field == "quantity"
    assert error.message == "Quantity must be numeric"


@pytest.mark.parametrize("quantity", ["0", "-5"], ids=["zero", "negative"])
def test_non_positive_quantity_is_rejected(quantity: str) -> None:
    csv_text = SCHEDULED_HEADER + "\n" + _scheduled_row(lot="TLC-QTY-NONPOS", quantity=quantity) + "\n"

    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.total == 1
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 2
    assert error.field == "quantity"
    assert error.message == "Quantity must be greater than 0"


# ---------------------------------------------------------------------------
# #98 — a non-finite quantity must be rejected at import, not persisted.
#
# float() accepts the literal tokens "nan"/"inf"/"-inf" and overflowing
# decimals like "1e400". NaN compares False against every ordering operator,
# so `nan <= 0` is False and the positivity branch above waves it through;
# infinities are genuinely > 0 and pass on the merits. Both then reach
# json.dumps, which emits a bare NaN/Infinity token -- not RFC 8259 -- into
# the tenant's durable JSONL and onto the signed wire.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quantity",
    ["nan", "NaN", "-nan", "inf", "Infinity", "-inf", "1e400"],
    ids=["nan", "nan_capitalized", "negative_nan", "inf", "infinity_word", "negative_inf", "overflow_literal"],
)
def test_non_finite_quantity_is_rejected(quantity: str) -> None:
    csv_text = SCHEDULED_HEADER + "\n" + _scheduled_row(lot="TLC-QTY-NONFINITE", quantity=quantity) + "\n"

    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.total == 1
    # No record is produced, so nothing can be delivered or persisted.
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 2
    assert error.field == "quantity"
    assert error.message == "Quantity must be a finite number"

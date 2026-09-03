from datetime import UTC, datetime

import pytest

from app.csv_importer import parse_csv_import
from app.schemas.domain import CSVImportType


def test_parse_seed_lot_uses_deterministic_default_timestamp():
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        """traceability_lot_code,product_description,quantity,unit_of_measure,location_name
TLC-SEED-DEFAULT,Romaine Hearts,42,cases,Valley Fresh Farms
""",
        default_timestamp=datetime(2026, 2, 7, 11, 30, tzinfo=UTC),
    )

    assert parsed.total == 1
    assert len(parsed.events) == 1
    assert parsed.errors == []
    event = parsed.events[0]
    assert event.timestamp == datetime(2026, 2, 7, 11, 30, tzinfo=UTC)
    assert event.kdes["harvest_date"] == "2026-02-07"
    assert event.kdes["reference_document_number"] == "CSV-TLC-SEED-DEFAULT"
    assert event.kdes["tlc_source_reference"] == "CSV-SEED-TLC-SEED-DEFAULT"


def test_parse_scheduled_event_reports_invalid_cte_timestamp_and_kdes():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,kdes
packing,TLC-BAD,Romaine Lettuce,10,cases,Coastal Packhouse,not-a-date,"[]"
""",
    )

    assert parsed.total == 1
    assert parsed.events == []
    assert [(error.row, error.field) for error in parsed.errors] == [
        (2, "timestamp"),
        (2, "cte_type"),
        (2, "kdes"),
    ]


def test_parse_scheduled_event_derives_parent_lots_from_kde_columns():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,kde_input_traceability_lot_codes
transformation,TLC-OUT,Fresh Cut Salad Mix,50,cases,ReadyFresh Processing Plant,2026-02-07T12:00:00Z,TLC-IN-1|TLC-IN-2
""",
    )

    assert parsed.total == 1
    assert parsed.errors == []
    assert parsed.parent_lot_codes == [["TLC-IN-1", "TLC-IN-2"]]
    assert parsed.events[0].kdes["input_traceability_lot_codes"] == ["TLC-IN-1", "TLC-IN-2"]
    assert {warning.field for warning in parsed.warnings} >= {
        "transformation_date",
        "reference_document",
        "reference_document_type",
    }


def test_parse_scheduled_event_warns_on_malformed_cte_kdes():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,kdes
transformation,TLC-OUT,Fresh Cut Salad Mix,50,cases,ReadyFresh Processing Plant,2026-02-07T12:00:00Z,"{""input_traceability_lot_codes"":{""lot"":""TLC-IN-1""}}"
""",
    )

    assert parsed.total == 1
    assert parsed.errors == []
    assert len(parsed.events) == 1
    assert {
        (warning.field, warning.message) for warning in parsed.warnings
    } >= {
        (
            "input_traceability_lot_codes",
            "Transformation input_traceability_lot_codes should be a non-empty list of lot codes",
        ),
        ("reference_document", "Missing expected transformation KDE: reference_document"),
    }


# --- Rejection branches (#129) --------------------------------------------
#
# The operator console's bulk-upload path is hand-edited CSV, so the branches
# below -- a blank file, a header typo, a bad quantity -- are the ones a real
# user hits first. Each test pins the branch's own `CSVImportError` row/field
# /message so the branch cannot be deleted or reworded unnoticed.

SEED_HEADER = "traceability_lot_code,product_description,quantity,unit_of_measure,location_name"


def _errors(parsed):
    return [(error.row, error.field, error.message) for error in parsed.errors]


@pytest.mark.parametrize("csv_text", ["", "   ", "\n\n", "  \n \t \n"])
def test_parse_rejects_empty_csv_body_before_reading_any_row(csv_text):
    parsed = parse_csv_import(CSVImportType.SEED_LOTS, csv_text)

    assert parsed.total == 0
    assert parsed.events == []
    assert parsed.parent_lot_codes == []
    assert _errors(parsed) == [(0, "csv_text", "CSV content is empty")]


def test_parse_rejects_header_only_csv_as_having_no_data_rows():
    parsed = parse_csv_import(CSVImportType.SEED_LOTS, SEED_HEADER + "\n")

    assert parsed.total == 0
    assert parsed.events == []
    assert _errors(parsed) == [(0, "csv_text", "CSV contains no data rows")]


def test_parse_rejects_csv_with_no_header_row():
    # A body that is non-blank but contributes no header at all: a lone
    # byte-order mark. `csv_text.strip()` sees content, so the empty-body
    # branch does not fire, and the reader is left with no fieldnames.
    parsed = parse_csv_import(CSVImportType.SEED_LOTS, "﻿")

    assert parsed.total == 0
    assert parsed.events == []
    assert _errors(parsed) == [(1, "header", "CSV header row is required")]


def test_parse_rejects_blank_header_name():
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        "traceability_lot_code,,quantity,unit_of_measure,location_name\n"
        "TLC-BLANK-HEADER,Romaine Hearts,10,cases,Valley Fresh Farms\n",
    )

    assert parsed.total == 0
    assert parsed.events == []
    assert _errors(parsed) == [(1, "header", "CSV header names cannot be blank")]


def test_parse_rejects_headers_that_collide_after_normalization():
    # `quantity` and `Quantity` are distinct to `csv.DictReader` but normalize
    # to the same key, so one would silently overwrite the other.
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        "traceability_lot_code,product_description,quantity,Quantity,unit_of_measure,location_name\n"
        "TLC-DUPE-HEADER,Romaine Hearts,10,12,cases,Valley Fresh Farms\n",
    )

    assert parsed.total == 0
    assert parsed.events == []
    assert _errors(parsed) == [
        (1, "header", "Duplicate CSV header after normalization: quantity")
    ]


def test_parse_rejects_non_numeric_quantity():
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        SEED_HEADER + "\nTLC-BAD-QTY,Romaine Hearts,abc,cases,Valley Fresh Farms\n",
    )

    # The row is counted as seen but produces no event.
    assert parsed.total == 1
    assert parsed.events == []
    assert _errors(parsed) == [(2, "quantity", "Quantity must be numeric")]


@pytest.mark.parametrize("quantity", ["0", "-5", "-0.25"])
def test_parse_rejects_zero_or_negative_quantity(quantity):
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        SEED_HEADER + f"\nTLC-BAD-QTY,Romaine Hearts,{quantity},cases,Valley Fresh Farms\n",
    )

    assert parsed.total == 1
    assert parsed.events == []
    assert _errors(parsed) == [(2, "quantity", "Quantity must be greater than 0")]

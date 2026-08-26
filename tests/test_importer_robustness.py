"""Regression tests for CSV importer robustness fixes (issues #160, #162).

#160: a data row with more fields than the header must be reported as a
per-row CSVImportError instead of having the surplus silently discarded.

#162: a CSV column must map to exactly one KDE key -- a bare column and its
kde_-prefixed twin colliding on the same target must be reported instead of
one silently overwriting the other -- and a `location_gln` column must
populate RegEngineEvent.location_gln instead of falling into
kdes["location_gln"], where no exporter can see it.

Kept separate from tests/test_csv_importer.py (untouched by this change) per
this task's file-ownership rules.
"""

from datetime import UTC, datetime

from app.csv_importer import parse_csv_import
from app.schemas.domain import CSVImportType


def _csv(header, *rows):
    """Build CSV text from a header and rows of explicit field lists.

    Joining field lists (rather than hand-typing comma-separated strings)
    sidesteps miscounting the blank placeholder columns the #162 collision
    tests below need to keep a single shared header.
    """
    lines = [",".join(header)]
    lines.extend(",".join(row) for row in rows)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# #160 -- rows with more columns than the header
# ---------------------------------------------------------------------------


def test_ragged_row_from_unquoted_embedded_comma_is_flagged_as_error():
    # An unescaped comma inside "Mixed Greens, baby kale" shifts every later
    # value one column to the right, leaving the timestamp value with
    # nowhere to go. That must surface as a per-row error, not vanish
    # silently the way it did before this fix.
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp
harvesting,TLC-RAGGED-1,Mixed Greens, baby kale,25,cases,Valley Fresh Farms,2026-02-07T12:00:00Z
""",
    )

    assert parsed.total == 1
    assert parsed.events == []
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.row == 2
    assert "column" in error.message.lower()


def test_ragged_row_does_not_swallow_a_well_formed_row_that_follows_it():
    # The bad row must be reported without disturbing row numbering or
    # rejecting the good row after it.
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp
harvesting,TLC-RAGGED-1,Mixed Greens, baby kale,25,cases,Valley Fresh Farms,2026-02-07T12:00:00Z
harvesting,TLC-GOOD-1,Romaine Hearts,25,cases,Valley Fresh Farms,2026-02-07T12:00:00Z
""",
    )

    assert parsed.total == 2
    assert [event.traceability_lot_code for event in parsed.events] == ["TLC-GOOD-1"]
    assert [(error.row, error.field) for error in parsed.errors] == [(2, "row")]


def test_benign_trailing_delimiter_with_no_extra_data_is_not_flagged():
    # A trailing comma with nothing after it (a common spreadsheet-export
    # artifact) parks one empty string in DictReader's overflow list. That's
    # not the "lost data" case #160 targets, so it must still import cleanly
    # rather than being rejected as ragged.
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        """traceability_lot_code,product_description,quantity,unit_of_measure,location_name
TLC-TRAIL-1,Romaine Hearts,10,cases,Valley Fresh Farms,
""",
    )

    assert parsed.total == 1
    assert parsed.errors == []
    assert len(parsed.events) == 1


def test_well_formed_multi_kde_row_still_imports_unchanged():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,reference_document_type,reference_document_number,source_traceability_lot_code
transformation,TLC-OUT-1,Fresh Cut Salad Mix,50,cases,ReadyFresh Processing Plant,2026-02-07T12:00:00Z,Bill of Lading,BOL-100,TLC-IN-1
""",
    )

    assert parsed.total == 1
    assert parsed.errors == []
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.kdes["reference_document_type"] == "Bill of Lading"
    assert event.kdes["reference_document_number"] == "BOL-100"
    assert event.kdes["source_traceability_lot_code"] == "TLC-IN-1"
    # No location_gln column was supplied -- the dedicated field must stay
    # unset rather than silently defaulting to an empty string.
    assert event.location_gln is None


# ---------------------------------------------------------------------------
# #162 -- ambiguous CSV column-to-KDE mapping
# ---------------------------------------------------------------------------

SCHEDULED_BASE = (
    "cte_type",
    "traceability_lot_code",
    "product_description",
    "quantity",
    "unit_of_measure",
    "location_name",
    "timestamp",
)


def test_bare_and_kde_prefixed_columns_colliding_are_flagged_regardless_of_order():
    # Two data rows share one header so both physical orderings -- the bare
    # column declared before its kde_ twin, and vice versa -- are exercised
    # without needing a second parse_csv_import call.
    header = SCHEDULED_BASE + ("vessel_identifier", "kde_vessel_identifier", "kde_batch_ref", "batch_ref")
    row_bare_declared_first = [
        "receiving", "TLC-COLLIDE-1", "Frozen Tuna Loins", "100", "cases",
        "Dockside Facility", "2026-02-07T12:00:00Z",
        "VESSEL-REAL-001", "VESSEL-BOGUS-999", "", "",
    ]
    row_prefixed_declared_first = [
        "receiving", "TLC-COLLIDE-2", "Frozen Tuna Loins", "100", "cases",
        "Dockside Facility", "2026-02-07T12:00:00Z",
        "", "", "BATCH-REAL-1", "BATCH-BOGUS-2",
    ]

    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        _csv(header, row_bare_declared_first, row_prefixed_declared_first),
    )

    assert parsed.total == 2
    assert parsed.events == []
    assert [error.row for error in parsed.errors] == [2, 3]
    assert "vessel_identifier" in parsed.errors[0].message
    assert "batch_ref" in parsed.errors[1].message


def test_bare_and_kde_prefixed_columns_each_map_to_expected_kde_when_used_alone():
    # Same column pair as above, but each row populates only one of the two
    # -- the legitimate, unambiguous use of either naming convention must
    # keep mapping to exactly the KDE intended.
    header = SCHEDULED_BASE + ("vessel_identifier", "kde_vessel_identifier")
    row_bare = [
        "receiving", "TLC-BARE-1", "Frozen Tuna Loins", "100", "cases",
        "Dockside Facility", "2026-02-07T12:00:00Z", "VESSEL-BARE-1", "",
    ]
    row_prefixed = [
        "receiving", "TLC-PREFIXED-1", "Frozen Tuna Loins", "100", "cases",
        "Dockside Facility", "2026-02-07T12:00:00Z", "", "VESSEL-PREFIXED-1",
    ]

    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        _csv(header, row_bare, row_prefixed),
    )

    assert parsed.errors == []
    assert len(parsed.events) == 2
    by_lot = {event.traceability_lot_code: event for event in parsed.events}
    assert by_lot["TLC-BARE-1"].kdes["vessel_identifier"] == "VESSEL-BARE-1"
    assert by_lot["TLC-PREFIXED-1"].kdes["vessel_identifier"] == "VESSEL-PREFIXED-1"
    assert "kde_vessel_identifier" not in by_lot["TLC-PREFIXED-1"].kdes


def test_location_gln_column_populates_dedicated_field_not_kdes():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,location_gln
receiving,TLC-GLN-1,Frozen Tuna Loins,100,cases,Dockside Facility,2026-02-07T12:00:00Z,0812345000013
""",
    )

    assert parsed.errors == []
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.location_gln == "0812345000013"
    assert "location_gln" not in event.kdes


def test_seed_lot_import_threads_location_gln_and_kde_columns_through_shared_path():
    # _parse_kdes and _build_event are shared by both import types -- confirm
    # the #162 fixes hold on the seed-lot path too, not just scheduled events.
    parsed = parse_csv_import(
        CSVImportType.SEED_LOTS,
        """traceability_lot_code,product_description,quantity,unit_of_measure,location_name,location_gln,kde_vessel_identifier
TLC-SEED-GLN-1,Romaine Hearts,42,cases,Valley Fresh Farms,0850000010017,VESSEL-SEED-1
""",
        default_timestamp=datetime(2026, 2, 7, 11, 30, tzinfo=UTC),
    )

    assert parsed.total == 1
    assert parsed.errors == []
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.location_gln == "0850000010017"
    assert event.kdes["vessel_identifier"] == "VESSEL-SEED-1"
    assert "location_gln" not in event.kdes

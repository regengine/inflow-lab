"""Regression coverage for issues #94 and #99.

#94 -- the FDA export's "Traceability Lot Code Description" column held
``event.cte_type.value`` (e.g. "receiving") while the real product
description sat in the adjacent "Product Description" column. Fixed by
renaming the mislabeled column to "Event Type (CTE)" rather than inventing
content for it, because no KDE named "Traceability Lot Code Description"
exists anywhere in FSMA 204 (21 CFR 1.1325-1.1350's per-CTE KDE lists
contain a "Product Description"/"the product description for the food" KDE
-- already its own column -- and nothing else description-shaped tied to
the lot code itself), and RegEngine's own canonical spreadsheet
(fsma_spreadsheet.py) has no lot-code-description column either, putting
the CTE under "Event Type (CTE)" instead. These tests render over real
``DEMO_FIXTURES`` data, like tests/test_conformance.py, so they fail the
same way a human opening the exported spreadsheet would notice the bug.

#99 -- ``parent_lot_codes`` is a ``CONTROL_FIELDS`` entry excluded from the
catch-all KDE sweep for both `scheduled_events` and `seed_lots` CSV
imports, but only ``_parse_scheduled_event`` ever reads it. Chosen
semantics: seed lots stay parentless. They always become ``harvesting``
events (README "CSV import" section: "Seed lots become valid `harvesting`
events"), and Harvesting is the CTE where a traceability lot code is first
established -- 21 CFR 1.1330's Harvesting KDEs (commodity/variety, date
received, farm location, harvester business name, date of harvesting) have
no source/parent-lot concept, and ``DEMO_FIXTURES`` follows the same rule:
every HARVESTING fixture event leaves ``parent_lot_codes`` at its default
empty tuple, only downstream CTEs (packing, shipping, ...) set it. So the
fix is not to honor ``parent_lot_codes`` for seed lots -- that would record
a parent on a CTE that structurally can't have one, corrupting the lineage
graph in store.py and the EPCIS export -- but to stop *silently* dropping a
caller-supplied value: a non-empty ``parent_lot_codes`` column on a
seed-lot row must now come back as an explicit warning naming the field,
matching option 2 in the issue.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from app.csv_importer import parse_csv_import
from app.demo_fixtures import DEMO_FIXTURES
from app.fda_export import FDA_EXPORT_COLUMNS, render_fda_request_csv
from app.schemas.domain import CSVImportType, CTEType, DemoFixtureId, StoredEventRecord


# ---------------------------------------------------------------------------
# #94 -- FDA export column mislabel
# ---------------------------------------------------------------------------


def _records_for(fixture_id: DemoFixtureId) -> list[StoredEventRecord]:
    fixture = DEMO_FIXTURES[fixture_id]
    return [
        StoredEventRecord(
            sequence_no=index,
            payload_source="test-export-import-fixes",
            event=fixture_event.event,
            parent_lot_codes=list(fixture_event.parent_lot_codes),
        )
        for index, fixture_event in enumerate(fixture.events, start=1)
    ]


def _no_gln(_location_name: str) -> str:
    return ""


def _fda_rows(records: list[StoredEventRecord]) -> list[dict[str, str]]:
    csv_text = render_fda_request_csv(records, location_gln=_no_gln)
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_fda_export_no_longer_has_a_column_named_for_content_it_does_not_carry():
    """Acceptance criterion from #94: no header describes content it lacks.

    "Traceability Lot Code Description" promised a lot-code description and
    delivered a CTE type -- it must be gone, not just re-populated under
    the same misleading name.
    """
    assert "Traceability Lot Code Description" not in FDA_EXPORT_COLUMNS
    assert "Event Type (CTE)" in FDA_EXPORT_COLUMNS

    records = _records_for(DemoFixtureId.LEAFY_GREENS_TRACE)
    header = next(csv.reader(io.StringIO(render_fda_request_csv(records, location_gln=_no_gln))))
    assert "Traceability Lot Code Description" not in header
    assert "Event Type (CTE)" in header


def test_fda_export_no_description_labeled_column_ever_holds_a_raw_cte_type():
    """The actual defect from #94, checked generically rather than just
    against the specific old header name: no column whose header contains
    "Description" may hold a raw CTE-type token. Run against the pre-fix
    code this would have failed on "Traceability Lot Code Description";
    against the fix, only genuine description columns remain and none of
    them carry a CTE-type value.
    """
    cte_type_values = {cte.value for cte in CTEType}
    for fixture_id in DemoFixtureId:
        for row in _fda_rows(_records_for(fixture_id)):
            description_columns = {header: value for header, value in row.items() if "Description" in header}
            assert description_columns, "expected at least one *Description* column in the export"
            for header, value in description_columns.items():
                assert value not in cte_type_values, (
                    f"{header!r} holds a raw CTE-type value {value!r} in row {row!r}"
                )


def test_fda_export_event_type_cte_column_holds_the_real_cte_type_per_row():
    """The CTE value the old column carried is still exported -- just under
    an honestly-named column -- alongside its own real Product Description.
    """
    records = _records_for(DemoFixtureId.LEAFY_GREENS_TRACE)
    rows = _fda_rows(records)
    assert len(rows) == len(records) and len(records) > 1  # sanity: exercises more than one CTE type

    for row, record in zip(rows, records):
        assert row["Event Type (CTE)"] == record.event.cte_type.value
        assert row["Product Description"] == record.event.product_description
        assert row["Product Description"] != row["Event Type (CTE)"]


def test_fda_export_still_renders_thirteen_columns():
    """The rename kept the column *count* unchanged (13, per issue #186 and
    the README) even though one header's name and meaning changed. Called
    out explicitly per this task's instructions, since RegEngine mirrors
    this export's shape and would need to know if the count ever moves.
    """
    assert len(FDA_EXPORT_COLUMNS) == 13


# ---------------------------------------------------------------------------
# #99 -- parent_lot_codes silently dropped on seed_lots imports
# ---------------------------------------------------------------------------


_SEED_DEFAULT_TIMESTAMP = datetime(2026, 2, 7, 11, 30, tzinfo=UTC)


def test_seed_lot_import_with_parent_lot_codes_is_not_recorded_but_warns_explicitly():
    """Chosen semantics for #99: seed lots stay parentless (see module
    docstring), but a non-empty parent_lot_codes column can no longer
    vanish without a trace the way it did before this fix.
    """
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,parent_lot_codes\n"
        "TLC-SEED-001,Romaine Hearts,42,cases,Valley Fresh Farms,"
        "TLC-UPSTREAM-1|TLC-UPSTREAM-2\n"
    )
    parsed = parse_csv_import(CSVImportType.SEED_LOTS, csv_text, default_timestamp=_SEED_DEFAULT_TIMESTAMP)

    assert parsed.errors == []
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.cte_type == CTEType.HARVESTING

    # Not recorded as lineage -- a seed lot's harvesting event has no parent.
    assert parsed.parent_lot_codes == [[]]
    # Not smuggled into kdes either -- parent_lot_codes stays a control
    # field for this import type just as it is for scheduled_events.
    assert "parent_lot_codes" not in event.kdes

    # But the drop is no longer silent: exactly one warning names the field.
    matching_warnings = [warning for warning in parsed.warnings if warning.field == "parent_lot_codes"]
    assert len(matching_warnings) == 1
    assert matching_warnings[0].row == 2
    assert "ignored" in matching_warnings[0].message
    assert "seed_lots" in matching_warnings[0].message


def test_seed_lot_import_without_parent_lot_codes_column_has_no_such_warning():
    """Sanity check on the other side: nothing supplied, nothing to warn
    about -- an ordinary seed-lot row is unaffected by this fix.
    """
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
        "TLC-SEED-002,Romaine Hearts,42,cases,Valley Fresh Farms\n"
    )
    parsed = parse_csv_import(CSVImportType.SEED_LOTS, csv_text, default_timestamp=_SEED_DEFAULT_TIMESTAMP)

    assert parsed.errors == []
    assert len(parsed.events) == 1
    assert parsed.parent_lot_codes == [[]]
    assert not any(warning.field == "parent_lot_codes" for warning in parsed.warnings)


def test_seed_lot_import_blank_parent_lot_codes_column_has_no_such_warning():
    """A present-but-empty column is not "supplied and lost" -- _normalize_row
    strips it to "", which is falsy, so it must not warn either.
    """
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,parent_lot_codes\n"
        "TLC-SEED-003,Romaine Hearts,42,cases,Valley Fresh Farms,\n"
    )
    parsed = parse_csv_import(CSVImportType.SEED_LOTS, csv_text, default_timestamp=_SEED_DEFAULT_TIMESTAMP)

    assert parsed.errors == []
    assert len(parsed.events) == 1
    assert not any(warning.field == "parent_lot_codes" for warning in parsed.warnings)


def test_scheduled_event_import_still_honors_parent_lot_codes_unlike_seed_lots():
    """Contrast case: `scheduled_events` is untouched by the #99 fix --
    parent_lot_codes is still derived and recorded for CTE types that can
    legitimately have an upstream lot (unlike Harvesting/seed lots), and
    carries no "ignored" warning the way the seed-lot path now does.
    """
    csv_text = (
        "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,timestamp,parent_lot_codes\n"
        "initial_packing,TLC-PACK-001,Romaine Lettuce,112,cases,Coastal Packhouse,"
        "2026-02-05T10:00:00Z,TLC-HARVEST-001\n"
    )
    parsed = parse_csv_import(CSVImportType.SCHEDULED_EVENTS, csv_text)

    assert parsed.errors == []
    assert len(parsed.events) == 1
    assert parsed.parent_lot_codes == [["TLC-HARVEST-001"]]
    assert not any(warning.field == "parent_lot_codes" for warning in parsed.warnings)

from datetime import UTC, datetime

from app.csv_importer import parse_csv_import
from app.models import CSVImportType


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

from datetime import UTC, datetime

from app.audit import audit_warnings_for_record
from app.cte_rules import evaluate_audit_checks, merged_event_values, validate_event_kdes
from app.scenarios import get_scenario
from app.schemas.domain import CTEType, StoredEventRecord, RegEngineEvent


def test_validate_event_kdes_uses_top_level_contract_fields():
    event = RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="urn:epc:id:sgtin:8500000.10001.260509000001",
        product_description="Romaine Lettuce",
        quantity=42.0,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        location_gln="0850000001001",
        timestamp=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        kdes={
            "harvest_date": "2026-05-09",
            "reference_document": "GS1 Harvest Log HAR-20260509-00001",
            "reference_document_type": "Harvest Log",
        },
    )

    warnings = validate_event_kdes(event)

    assert not {warning.field for warning in warnings} & {
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "location_name",
    }


def test_merged_event_values_bridges_source_reference_aliases():
    event = RegEngineEvent(
        cte_type=CTEType.SHIPPING,
        traceability_lot_code="TLC-1",
        product_description="Salmon",
        quantity=10.0,
        unit_of_measure="cases",
        location_name="Dock",
        timestamp=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        kdes={
            "ship_date": "2026-05-09",
            "ship_from_location": "Dock",
            "ship_to_location": "DC",
            "reference_document": "GS1-128 (00)123456789012345678",
            "traceability_lot_code_source_reference": "SRC-1",
        },
    )

    merged = merged_event_values(event)

    assert merged["tlc_source_reference"] == "SRC-1"
    assert merged["traceability_lot_code_source_reference"] == "SRC-1"


def test_dairy_audit_uses_shared_rules_for_checks_and_warnings():
    scenario = get_scenario("dairy_continuous_flow")
    record = StoredEventRecord(
        payload_source="test",
        event=RegEngineEvent(
            cte_type=CTEType.INITIAL_PACKING,
            traceability_lot_code="TLC-DAIRY-1",
            product_description="Raw Milk",
            quantity=1000.0,
            unit_of_measure="gallons",
            location_name="Creamline Vat Hall",
            location_gln="0850000003201",
            timestamp=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            kdes={
                "packing_date": "2026-05-09",
                "reference_document": "Milk Collection Log MILK-1",
                "harvester_business_name": "Heritage Dairy Cooperative",
                "flow_type": "batch",
            },
        ),
    )

    warnings = audit_warnings_for_record(record, scenario)
    checks = evaluate_audit_checks([record], scenario)

    assert any(warning.field == "flow_type" for warning in warnings)
    assert any(warning.field == "silo_identifier" for warning in warnings)
    assert any(check["label"] == "Continuous flow KDEs" and not check["ok"] for check in checks)

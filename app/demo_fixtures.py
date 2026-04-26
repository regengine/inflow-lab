from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import CTEType, DemoFixtureId, RegEngineEvent
from .scenarios import ScenarioId


@dataclass(frozen=True, slots=True)
class DemoFixtureEvent:
    event: RegEngineEvent
    parent_lot_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DemoFixture:
    id: DemoFixtureId
    label: str
    description: str
    scenario: ScenarioId
    events: tuple[DemoFixtureEvent, ...]

    @property
    def lot_codes(self) -> list[str]:
        lot_codes = []
        for fixture_event in self.events:
            lot_code = fixture_event.event.traceability_lot_code
            if lot_code not in lot_codes:
                lot_codes.append(lot_code)
        return lot_codes


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def event(
    cte_type: CTEType,
    lot_code: str,
    product_description: str,
    quantity: float,
    unit_of_measure: str,
    location_name: str,
    timestamp: str,
    kdes: dict,
) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=cte_type,
        traceability_lot_code=lot_code,
        product_description=product_description,
        quantity=quantity,
        unit_of_measure=unit_of_measure,
        location_name=location_name,
        timestamp=dt(timestamp),
        kdes=kdes,
    )


DEMO_FIXTURES: dict[DemoFixtureId, DemoFixture] = {
    DemoFixtureId.LEAFY_GREENS_TRACE: DemoFixture(
        id=DemoFixtureId.LEAFY_GREENS_TRACE,
        label="Leafy greens trace",
        description="Harvest through cooling, packout, shipment, and DC receipt for one leafy greens lot.",
        scenario=ScenarioId.LEAFY_GREENS_SUPPLIER,
        events=(
            DemoFixtureEvent(
                event(
                    CTEType.HARVESTING,
                    "TLC-DEMO-LG-HARVEST-001",
                    "Romaine Lettuce",
                    420,
                    "cases",
                    "Valley Fresh Farms",
                    "2026-02-05T08:00:00Z",
                    {
                        "harvest_date": "2026-02-05",
                        "farm_location": "Valley Fresh Farms",
                        "field_name": "Field-7",
                        "immediate_subsequent_recipient": "Salinas Cooling Hub",
                        "reference_document_type": "Harvest Log",
                        "reference_document_number": "HAR-DEMO-LG-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-LG-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.COOLING,
                    "TLC-DEMO-LG-HARVEST-001",
                    "Romaine Lettuce",
                    420,
                    "cases",
                    "Salinas Cooling Hub",
                    "2026-02-05T09:10:00Z",
                    {
                        "cooling_date": "2026-02-05",
                        "cooling_location": "Salinas Cooling Hub",
                        "harvest_location": "Valley Fresh Farms",
                        "reference_document_type": "Cooling Log",
                        "reference_document_number": "COOL-DEMO-LG-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-LG-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.INITIAL_PACKING,
                    "TLC-DEMO-LG-PACK-001",
                    "Romaine Lettuce",
                    396,
                    "cases",
                    "FreshPack Central",
                    "2026-02-05T11:30:00Z",
                    {
                        "pack_date": "2026-02-05",
                        "packing_location": "FreshPack Central",
                        "source_traceability_lot_code": "TLC-DEMO-LG-HARVEST-001",
                        "farm_location": "Valley Fresh Farms",
                        "reference_document_type": "Packout Record",
                        "reference_document_number": "PACK-DEMO-LG-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-LG-PACK-001",
                    },
                ),
                parent_lot_codes=("TLC-DEMO-LG-HARVEST-001",),
            ),
            DemoFixtureEvent(
                event(
                    CTEType.SHIPPING,
                    "TLC-DEMO-LG-PACK-001",
                    "Romaine Lettuce",
                    396,
                    "cases",
                    "FreshPack Central",
                    "2026-02-05T14:00:00Z",
                    {
                        "ship_date": "2026-02-05",
                        "ship_from_location": "FreshPack Central",
                        "ship_to_location": "Distribution Center #4",
                        "carrier": "ColdRoute Freight",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-LG-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-LG-PACK-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.RECEIVING,
                    "TLC-DEMO-LG-PACK-001",
                    "Romaine Lettuce",
                    396,
                    "cases",
                    "Distribution Center #4",
                    "2026-02-05T19:15:00Z",
                    {
                        "receive_date": "2026-02-05",
                        "receiving_location": "Distribution Center #4",
                        "ship_from_location": "FreshPack Central",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-LG-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-LG-PACK-001",
                    },
                )
            ),
        ),
    ),
    DemoFixtureId.FRESH_CUT_TRANSFORMATION: DemoFixture(
        id=DemoFixtureId.FRESH_CUT_TRANSFORMATION,
        label="Fresh-cut transformation",
        description="Two ingredient lots received by a processor and transformed into a fresh-cut output lot.",
        scenario=ScenarioId.FRESH_CUT_PROCESSOR,
        events=(
            DemoFixtureEvent(
                event(
                    CTEType.HARVESTING,
                    "TLC-DEMO-FC-HARVEST-001",
                    "Romaine Lettuce",
                    300,
                    "cases",
                    "Valley Fresh Farms",
                    "2026-02-06T07:45:00Z",
                    {
                        "harvest_date": "2026-02-06",
                        "farm_location": "Valley Fresh Farms",
                        "field_name": "Field-3",
                        "immediate_subsequent_recipient": "Salinas Cooling Hub",
                        "reference_document_type": "Harvest Log",
                        "reference_document_number": "HAR-DEMO-FC-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.HARVESTING,
                    "TLC-DEMO-FC-HARVEST-002",
                    "Spinach",
                    260,
                    "cases",
                    "Coastal Leaf Farm",
                    "2026-02-06T08:05:00Z",
                    {
                        "harvest_date": "2026-02-06",
                        "farm_location": "Coastal Leaf Farm",
                        "field_name": "Field-11",
                        "immediate_subsequent_recipient": "Coastal Cold Chain",
                        "reference_document_type": "Harvest Log",
                        "reference_document_number": "HAR-DEMO-FC-002",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-002",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.COOLING,
                    "TLC-DEMO-FC-HARVEST-001",
                    "Romaine Lettuce",
                    300,
                    "cases",
                    "Salinas Cooling Hub",
                    "2026-02-06T09:10:00Z",
                    {
                        "cooling_date": "2026-02-06",
                        "cooling_location": "Salinas Cooling Hub",
                        "harvest_location": "Valley Fresh Farms",
                        "reference_document_type": "Cooling Log",
                        "reference_document_number": "COOL-DEMO-FC-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.COOLING,
                    "TLC-DEMO-FC-HARVEST-002",
                    "Spinach",
                    260,
                    "cases",
                    "Coastal Cold Chain",
                    "2026-02-06T09:35:00Z",
                    {
                        "cooling_date": "2026-02-06",
                        "cooling_location": "Coastal Cold Chain",
                        "harvest_location": "Coastal Leaf Farm",
                        "reference_document_type": "Cooling Log",
                        "reference_document_number": "COOL-DEMO-FC-002",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-002",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.INITIAL_PACKING,
                    "TLC-DEMO-FC-PACK-001",
                    "Romaine Lettuce",
                    288,
                    "cases",
                    "Processor Intake Packout",
                    "2026-02-06T11:00:00Z",
                    {
                        "pack_date": "2026-02-06",
                        "packing_location": "Processor Intake Packout",
                        "source_traceability_lot_code": "TLC-DEMO-FC-HARVEST-001",
                        "farm_location": "Valley Fresh Farms",
                        "reference_document_type": "Packout Record",
                        "reference_document_number": "PACK-DEMO-FC-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-PACK-001",
                    },
                ),
                parent_lot_codes=("TLC-DEMO-FC-HARVEST-001",),
            ),
            DemoFixtureEvent(
                event(
                    CTEType.INITIAL_PACKING,
                    "TLC-DEMO-FC-PACK-002",
                    "Spinach",
                    248,
                    "cases",
                    "Processor Intake Packout",
                    "2026-02-06T11:20:00Z",
                    {
                        "pack_date": "2026-02-06",
                        "packing_location": "Processor Intake Packout",
                        "source_traceability_lot_code": "TLC-DEMO-FC-HARVEST-002",
                        "farm_location": "Coastal Leaf Farm",
                        "reference_document_type": "Packout Record",
                        "reference_document_number": "PACK-DEMO-FC-002",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-PACK-002",
                    },
                ),
                parent_lot_codes=("TLC-DEMO-FC-HARVEST-002",),
            ),
            DemoFixtureEvent(
                event(
                    CTEType.SHIPPING,
                    "TLC-DEMO-FC-PACK-001",
                    "Romaine Lettuce",
                    288,
                    "cases",
                    "Processor Intake Packout",
                    "2026-02-06T12:10:00Z",
                    {
                        "ship_date": "2026-02-06",
                        "ship_from_location": "Processor Intake Packout",
                        "ship_to_location": "ReadyFresh Processing Plant",
                        "carrier": "PrepLine Logistics",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-FC-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-PACK-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.SHIPPING,
                    "TLC-DEMO-FC-PACK-002",
                    "Spinach",
                    248,
                    "cases",
                    "Processor Intake Packout",
                    "2026-02-06T12:15:00Z",
                    {
                        "ship_date": "2026-02-06",
                        "ship_from_location": "Processor Intake Packout",
                        "ship_to_location": "ReadyFresh Processing Plant",
                        "carrier": "PrepLine Logistics",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-FC-002",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-PACK-002",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.RECEIVING,
                    "TLC-DEMO-FC-PACK-001",
                    "Romaine Lettuce",
                    288,
                    "cases",
                    "ReadyFresh Processing Plant",
                    "2026-02-06T14:30:00Z",
                    {
                        "receive_date": "2026-02-06",
                        "receiving_location": "ReadyFresh Processing Plant",
                        "ship_from_location": "Processor Intake Packout",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-FC-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-PACK-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.RECEIVING,
                    "TLC-DEMO-FC-PACK-002",
                    "Spinach",
                    248,
                    "cases",
                    "ReadyFresh Processing Plant",
                    "2026-02-06T14:40:00Z",
                    {
                        "receive_date": "2026-02-06",
                        "receiving_location": "ReadyFresh Processing Plant",
                        "ship_from_location": "Processor Intake Packout",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-FC-002",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-PACK-002",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.TRANSFORMATION,
                    "TLC-DEMO-FC-OUT-001",
                    "Fresh Cut Salad Mix",
                    430,
                    "cases",
                    "ReadyFresh Processing Plant",
                    "2026-02-06T17:20:00Z",
                    {
                        "transformation_date": "2026-02-06",
                        "transformation_location": "ReadyFresh Processing Plant",
                        "input_traceability_lot_codes": [
                            "TLC-DEMO-FC-PACK-001",
                            "TLC-DEMO-FC-PACK-002",
                        ],
                        "input_products": ["Romaine Lettuce", "Spinach"],
                        "reference_document_type": "Batch Record",
                        "reference_document_number": "BATCH-DEMO-FC-001",
                        "yield_ratio": 0.802,
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-OUT-001",
                    },
                ),
                parent_lot_codes=("TLC-DEMO-FC-PACK-001", "TLC-DEMO-FC-PACK-002"),
            ),
            DemoFixtureEvent(
                event(
                    CTEType.SHIPPING,
                    "TLC-DEMO-FC-OUT-001",
                    "Fresh Cut Salad Mix",
                    430,
                    "cases",
                    "ReadyFresh Processing Plant",
                    "2026-02-06T20:00:00Z",
                    {
                        "ship_date": "2026-02-06",
                        "ship_from_location": "ReadyFresh Processing Plant",
                        "ship_to_location": "Foodservice DC #12",
                        "carrier": "ColdRoute Freight",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-FC-OUT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-OUT-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.RECEIVING,
                    "TLC-DEMO-FC-OUT-001",
                    "Fresh Cut Salad Mix",
                    430,
                    "cases",
                    "Foodservice DC #12",
                    "2026-02-07T02:00:00Z",
                    {
                        "receive_date": "2026-02-07",
                        "receiving_location": "Foodservice DC #12",
                        "ship_from_location": "ReadyFresh Processing Plant",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-FC-OUT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-FC-OUT-001",
                    },
                )
            ),
        ),
    ),
    DemoFixtureId.RETAILER_HANDOFF: DemoFixture(
        id=DemoFixtureId.RETAILER_HANDOFF,
        label="Retailer handoff",
        description="Retail-ready cases moving through DC receipt, outbound shipment, and store receipt.",
        scenario=ScenarioId.RETAILER_READINESS_DEMO,
        events=(
            DemoFixtureEvent(
                event(
                    CTEType.HARVESTING,
                    "TLC-DEMO-RT-HARVEST-001",
                    "Baby Spinach Clamshells",
                    240,
                    "cases",
                    "SunCoast Produce Ranch",
                    "2026-02-08T07:30:00Z",
                    {
                        "harvest_date": "2026-02-08",
                        "farm_location": "SunCoast Produce Ranch",
                        "field_name": "Field-2",
                        "immediate_subsequent_recipient": "Retail Cold Dock West",
                        "reference_document_type": "Harvest Log",
                        "reference_document_number": "HAR-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.COOLING,
                    "TLC-DEMO-RT-HARVEST-001",
                    "Baby Spinach Clamshells",
                    240,
                    "cases",
                    "Retail Cold Dock West",
                    "2026-02-08T08:20:00Z",
                    {
                        "cooling_date": "2026-02-08",
                        "cooling_location": "Retail Cold Dock West",
                        "harvest_location": "SunCoast Produce Ranch",
                        "reference_document_type": "Cooling Log",
                        "reference_document_number": "COOL-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.INITIAL_PACKING,
                    "TLC-DEMO-RT-PACK-001",
                    "Baby Spinach Clamshells",
                    228,
                    "cases",
                    "Retail Ready Packout",
                    "2026-02-08T10:00:00Z",
                    {
                        "pack_date": "2026-02-08",
                        "packing_location": "Retail Ready Packout",
                        "source_traceability_lot_code": "TLC-DEMO-RT-HARVEST-001",
                        "farm_location": "SunCoast Produce Ranch",
                        "reference_document_type": "Packout Record",
                        "reference_document_number": "PACK-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-PACK-001",
                    },
                ),
                parent_lot_codes=("TLC-DEMO-RT-HARVEST-001",),
            ),
            DemoFixtureEvent(
                event(
                    CTEType.SHIPPING,
                    "TLC-DEMO-RT-PACK-001",
                    "Baby Spinach Clamshells",
                    228,
                    "cases",
                    "Retail Ready Packout",
                    "2026-02-08T12:15:00Z",
                    {
                        "ship_date": "2026-02-08",
                        "ship_from_location": "Retail Ready Packout",
                        "ship_to_location": "Retail DC West",
                        "carrier": "StoreLane Logistics",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-PACK-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.RECEIVING,
                    "TLC-DEMO-RT-PACK-001",
                    "Baby Spinach Clamshells",
                    228,
                    "cases",
                    "Retail DC West",
                    "2026-02-08T16:00:00Z",
                    {
                        "receive_date": "2026-02-08",
                        "receiving_location": "Retail DC West",
                        "ship_from_location": "Retail Ready Packout",
                        "reference_document_type": "Bill of Lading",
                        "reference_document_number": "BOL-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-PACK-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.SHIPPING,
                    "TLC-DEMO-RT-PACK-001",
                    "Baby Spinach Clamshells",
                    228,
                    "cases",
                    "Retail DC West",
                    "2026-02-08T18:30:00Z",
                    {
                        "ship_date": "2026-02-08",
                        "ship_from_location": "Retail DC West",
                        "ship_to_location": "Retail Store #4521",
                        "carrier": "StoreLane Logistics",
                        "reference_document_type": "Transfer Order",
                        "reference_document_number": "TO-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-PACK-001",
                    },
                )
            ),
            DemoFixtureEvent(
                event(
                    CTEType.RECEIVING,
                    "TLC-DEMO-RT-PACK-001",
                    "Baby Spinach Clamshells",
                    228,
                    "cases",
                    "Retail Store #4521",
                    "2026-02-09T05:15:00Z",
                    {
                        "receive_date": "2026-02-09",
                        "receiving_location": "Retail Store #4521",
                        "ship_from_location": "Retail DC West",
                        "reference_document_type": "Transfer Order",
                        "reference_document_number": "TO-DEMO-RT-001",
                        "traceability_lot_code_source_reference": "SRC-DEMO-RT-PACK-001",
                    },
                )
            ),
        ),
    ),
}


def get_demo_fixture(fixture_id: DemoFixtureId | str) -> DemoFixture:
    return DEMO_FIXTURES[DemoFixtureId(fixture_id)]


def list_demo_fixture_summaries() -> list[dict[str, object]]:
    return [
        {
            "id": fixture.id.value,
            "label": fixture.label,
            "description": fixture.description,
            "scenario": fixture.scenario.value,
            "event_count": len(fixture.events),
            "lot_codes": fixture.lot_codes,
        }
        for fixture in DEMO_FIXTURES.values()
    ]

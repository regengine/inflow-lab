from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .schemas.domain import CTEType


class ScenarioId(str, Enum):
    LEAFY_GREENS_SUPPLIER = "leafy_greens_supplier"
    FRESH_CUT_PROCESSOR = "fresh_cut_processor"
    RETAILER_READINESS_DEMO = "retailer_readiness_demo"
    SEAFOOD_FIRST_RECEIVER = "seafood_first_receiver"
    DAIRY_CONTINUOUS_FLOW = "dairy_continuous_flow"


@dataclass(frozen=True, slots=True)
class Location:
    name: str
    location_type: str
    gln: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProductSpec:
    name: str
    unit: str
    category: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScenarioPreset:
    id: ScenarioId
    label: str
    description: str
    farms: tuple[Location, ...]
    coolers: tuple[Location, ...]
    packers: tuple[Location, ...]
    processors: tuple[Location, ...]
    dcs: tuple[Location, ...]
    retailers: tuple[Location, ...]
    products: tuple[ProductSpec, ...]
    transformation_outputs: tuple[str, ...]
    carriers: tuple[str, ...]
    action_weights: Mapping[str, int]
    industry_type: str
    operation_type: str
    reference_format: str
    source_cte_type: CTEType = CTEType.HARVESTING
    requires_cooling: bool = True
    harvest_target: int = 3
    packed_to_processor_probability: float = 0.45
    transform_input_choices: tuple[int, ...] = (2, 2, 3)

    @property
    def transform_min_lots(self) -> int:
        return min(self.transform_input_choices)


def _gln(location_id: int) -> str:
    body = f"0850000{location_id:05d}"
    total = sum(
        int(digit) * (1 if index % 2 else 3)
        for index, digit in enumerate(reversed(body))
    )
    check_digit = (10 - (total % 10)) % 10
    return f"{body}{check_digit}"


SCENARIO_PRESETS: dict[ScenarioId, ScenarioPreset] = {
    ScenarioId.LEAFY_GREENS_SUPPLIER: ScenarioPreset(
        id=ScenarioId.LEAFY_GREENS_SUPPLIER,
        label="Leafy greens supplier",
        description="Farm-origin leafy greens flowing through cooling, packout, and outbound cold-chain shipments.",
        farms=(
            Location("Valley Fresh Farms", "farm", _gln(1001), {"gps": "36.6777,-121.6555"}),
            Location("Desert Bloom Farm", "farm", _gln(1002), {"gps": "32.8473,-115.5671"}),
            Location("Riverbend Organics", "farm", _gln(1003), {"gps": "38.3149,-121.9018"}),
        ),
        coolers=(
            Location("Salinas Cooling Hub", "cooler", _gln(2001)),
            Location("Imperial Pre-Cool Facility", "cooler", _gln(2002)),
        ),
        packers=(
            Location("FreshPack Central", "packer", _gln(3001)),
            Location("GreenLeaf Packing House", "packer", _gln(3002)),
        ),
        processors=(
            Location("ReadyFresh Processing Plant", "processor", _gln(4001)),
            Location("DeliMix Plant", "processor", _gln(4002)),
        ),
        dcs=(
            Location("Distribution Center #4", "dc", _gln(5001)),
            Location("Distribution Center #7", "dc", _gln(5002)),
        ),
        retailers=(
            Location("Retail Store #4521", "retail", _gln(6001)),
            Location("Retail Store #3189", "retail", _gln(6002)),
        ),
        products=(
            ProductSpec("Romaine Lettuce", "cases", "leafy_greens", {"plu": "4640"}),
            ProductSpec("Green Leaf Lettuce", "cases", "leafy_greens", {"plu": "4065"}),
            ProductSpec("Spinach", "cases", "leafy_greens", {"plu": "4090"}),
            ProductSpec("Spring Mix", "cases", "leafy_greens", {"plu": "4321"}),
        ),
        transformation_outputs=(
            "Leafy Greens Blend",
            "Romaine Chopped Salad Base",
            "Spring Mix Salad Base",
        ),
        carriers=("SwiftChain Logistics", "ColdRoute Freight", "BlueLine Transport"),
        action_weights={
            "harvest": 5,
            "cool": 4,
            "initial_pack": 4,
            "ship": 4,
            "receive": 5,
            "transform": 4,
        },
        industry_type="produce",
        operation_type="supplier",
        reference_format="GS1",
        harvest_target=3,
        packed_to_processor_probability=0.45,
    ),
    ScenarioId.FRESH_CUT_PROCESSOR: ScenarioPreset(
        id=ScenarioId.FRESH_CUT_PROCESSOR,
        label="Fresh-cut processor",
        description="Ingredient lots routed into processor inventory, transformed into fresh-cut outputs, then shipped onward.",
        farms=(
            Location("Valley Fresh Farms", "farm", _gln(1001), {"gps": "36.6777,-121.6555"}),
            Location("Coastal Leaf Farm", "farm", _gln(1011), {"gps": "36.6039,-121.8947"}),
            Location("Desert Bloom Farm", "farm", _gln(1002), {"gps": "32.8473,-115.5671"}),
        ),
        coolers=(
            Location("Salinas Cooling Hub", "cooler", _gln(2001)),
            Location("Coastal Cold Chain", "cooler", _gln(2011)),
        ),
        packers=(
            Location("Processor Intake Packout", "packer", _gln(3011)),
            Location("GreenLeaf Packing House", "packer", _gln(3002)),
        ),
        processors=(
            Location("ReadyFresh Processing Plant", "processor", _gln(4001)),
            Location("Urban Greens Fresh-Cut", "processor", _gln(4011)),
        ),
        dcs=(
            Location("Foodservice DC #12", "dc", _gln(5011)),
            Location("Regional Cold DC #3", "dc", _gln(5012)),
        ),
        retailers=(
            Location("Cafe Commissary #88", "retail", _gln(6011)),
            Location("Retail Store #4521", "retail", _gln(6001)),
        ),
        products=(
            ProductSpec("Romaine Lettuce", "cases", "leafy_greens", {"plu": "4640"}),
            ProductSpec("Spinach", "cases", "leafy_greens", {"plu": "4090"}),
            ProductSpec("Green Cabbage", "cases", "produce", {"plu": "4069"}),
            ProductSpec("Shredding Carrots", "cases", "produce", {"plu": "4562"}),
        ),
        transformation_outputs=(
            "Fresh Cut Salad Mix",
            "Caesar Salad Kit",
            "Deli Salad Base",
            "Shredded Lettuce Foodservice Pack",
        ),
        carriers=("ColdRoute Freight", "PrepLine Logistics", "SwiftChain Logistics"),
        action_weights={
            "harvest": 4,
            "cool": 4,
            "initial_pack": 5,
            "ship": 6,
            "receive": 6,
            "transform": 8,
        },
        industry_type="produce",
        operation_type="processor",
        reference_format="GS1",
        harvest_target=4,
        packed_to_processor_probability=0.85,
        transform_input_choices=(2, 2, 3),
    ),
    ScenarioId.RETAILER_READINESS_DEMO: ScenarioPreset(
        id=ScenarioId.RETAILER_READINESS_DEMO,
        label="Retailer readiness demo",
        description="Retail-ready produce flows quickly through DC receiving and store-level downstream receipts.",
        farms=(
            Location("SunCoast Produce Ranch", "farm", _gln(1021), {"gps": "34.1231,-119.1802"}),
            Location("Valley Fresh Farms", "farm", _gln(1001), {"gps": "36.6777,-121.6555"}),
            Location("Riverbend Organics", "farm", _gln(1003), {"gps": "38.3149,-121.9018"}),
        ),
        coolers=(
            Location("Retail Cold Dock West", "cooler", _gln(2021)),
            Location("Salinas Cooling Hub", "cooler", _gln(2001)),
        ),
        packers=(
            Location("Retail Ready Packout", "packer", _gln(3021)),
            Location("FreshPack Central", "packer", _gln(3001)),
        ),
        processors=(
            Location("ReadyFresh Processing Plant", "processor", _gln(4001)),
            Location("Meal Kit Prep Center", "processor", _gln(4021)),
        ),
        dcs=(
            Location("Retail DC West", "dc", _gln(5021)),
            Location("Retail DC East", "dc", _gln(5022)),
        ),
        retailers=(
            Location("Retail Store #4521", "retail", _gln(6001)),
            Location("Retail Store #3189", "retail", _gln(6002)),
            Location("Urban Market #22", "retail", _gln(6021)),
        ),
        products=(
            ProductSpec("Romaine Hearts Retail Pack", "cases", "retail_ready", {"plu": "3097"}),
            ProductSpec("Baby Spinach Clamshells", "cases", "retail_ready", {"plu": "4090"}),
            ProductSpec("Spring Mix Clamshells", "cases", "retail_ready", {"plu": "4321"}),
            ProductSpec("Cucumber Snack Packs", "cases", "retail_ready", {"plu": "4593"}),
        ),
        transformation_outputs=(
            "Retail Caesar Salad Kit",
            "Grab-and-Go Salad Bowl",
            "Retail Chopped Greens Kit",
        ),
        carriers=("StoreLane Logistics", "ColdRoute Freight", "BlueLine Transport"),
        action_weights={
            "harvest": 4,
            "cool": 3,
            "initial_pack": 5,
            "ship": 7,
            "receive": 8,
            "transform": 2,
        },
        industry_type="produce",
        operation_type="retailer",
        reference_format="GS1",
        harvest_target=3,
        packed_to_processor_probability=0.1,
        transform_input_choices=(2, 2),
    ),
    ScenarioId.SEAFOOD_FIRST_RECEIVER: ScenarioPreset(
        id=ScenarioId.SEAFOOD_FIRST_RECEIVER,
        label="Seafood first receiver",
        description="Wild-caught seafood landed at the dock, first land-based received, portioned, and shipped with vessel-linked records.",
        farms=(),
        coolers=(
            Location("Kodiak First Receiver Dock", "first_receiver", _gln(2101), {"harbor": "Kodiak"}),
            Location("Monterey Harbor Seafood Dock", "first_receiver", _gln(2102), {"harbor": "Monterey"}),
        ),
        packers=(
            Location("North Pacific Seafood Packhouse", "packer", _gln(3101)),
            Location("Harbor Portioning Line", "packer", _gln(3102)),
        ),
        processors=(
            Location("ColdWave Fillet Plant", "processor", _gln(4101)),
            Location("Pacific Smokehouse", "processor", _gln(4102)),
        ),
        dcs=(
            Location("Seafood DC West", "dc", _gln(5101)),
            Location("Seafood Export Consolidation Hub", "dc", _gln(5102)),
        ),
        retailers=(
            Location("Harbor Market Seafood Counter", "retail", _gln(6101)),
            Location("Chef Supply Seafood Depot", "retail", _gln(6102)),
        ),
        products=(
            ProductSpec("Sockeye Salmon", "totes", "seafood", {"species_code": "SAL-SOC"}),
            ProductSpec("Pacific Cod", "totes", "seafood", {"species_code": "COD-PAC"}),
            ProductSpec("Black Cod", "totes", "seafood", {"species_code": "COD-BLK"}),
        ),
        transformation_outputs=(
            "Portioned Salmon Fillet Case",
            "Skin-On Cod Loin Case",
            "Smoked Seafood Batch",
        ),
        carriers=("NorthPort Reefer", "HarborLine Logistics", "BlueCurrent Marine Freight"),
        action_weights={
            "harvest": 5,
            "cool": 0,
            "initial_pack": 5,
            "ship": 5,
            "receive": 6,
            "transform": 6,
        },
        industry_type="seafood",
        operation_type="first_receiver",
        reference_format="GS1",
        source_cte_type=CTEType.FIRST_LAND_BASED_RECEIVING,
        requires_cooling=False,
        harvest_target=4,
        packed_to_processor_probability=0.65,
        transform_input_choices=(2, 3, 3),
    ),
    ScenarioId.DAIRY_CONTINUOUS_FLOW: ScenarioPreset(
        id=ScenarioId.DAIRY_CONTINUOUS_FLOW,
        label="Dairy continuous flow",
        description="Raw milk flows from farm silos into processing vats, is blended in continuous runs, and ships as production-ready dairy inventory.",
        farms=(
            Location("North Valley Dairy", "farm", _gln(1101), {"gps": "37.4322,-120.7605"}),
            Location("Sierra Crest Creamery", "farm", _gln(1102), {"gps": "36.9811,-119.7094"}),
        ),
        coolers=(),
        packers=(
            Location("Creamery Fill Hall", "packer", _gln(3201)),
            Location("Cultured Dairy Packaging", "packer", _gln(3202)),
        ),
        processors=(
            Location("Central Pasteurization Plant", "processor", _gln(4201)),
            Location("Aged Cheese Vat Room", "processor", _gln(4202)),
        ),
        dcs=(
            Location("Dairy Cold Storage North", "dc", _gln(5201)),
            Location("Foodservice Dairy DC", "dc", _gln(5202)),
        ),
        retailers=(
            Location("Regional Grocery Dairy Depot", "retail", _gln(6201)),
            Location("Ingredient Buyer Warehouse", "retail", _gln(6202)),
        ),
        products=(
            ProductSpec("Raw Whole Milk", "gallons", "dairy", {"fat_test": "3.7"}),
            ProductSpec("Raw Skim Milk", "gallons", "dairy", {"fat_test": "0.2"}),
            ProductSpec("Cream Stream", "gallons", "dairy", {"fat_test": "18.0"}),
        ),
        transformation_outputs=(
            "Cultured Dairy Blend",
            "Cheddar Vat Run",
            "Pasteurized Milk Tote",
        ),
        carriers=("WhiteLine Refrigerated", "CreamRoute Logistics", "ColdRoute Freight"),
        action_weights={
            "harvest": 5,
            "cool": 0,
            "initial_pack": 4,
            "ship": 5,
            "receive": 6,
            "transform": 7,
        },
        industry_type="dairy",
        operation_type="processor",
        reference_format="GS1",
        requires_cooling=False,
        harvest_target=4,
        packed_to_processor_probability=0.9,
        transform_input_choices=(2, 3, 3),
    ),
}


def get_scenario(scenario_id: ScenarioId | str | None = None) -> ScenarioPreset:
    normalized = ScenarioId(scenario_id or ScenarioId.LEAFY_GREENS_SUPPLIER)
    return SCENARIO_PRESETS[normalized]


def list_scenario_summaries() -> list[dict[str, str | bool]]:
    return [
        {
            "id": preset.id.value,
            "label": preset.label,
            "description": preset.description,
            "industry_type": preset.industry_type,
            "operation_type": preset.operation_type,
            "reference_format": preset.reference_format,
            "requires_cooling": preset.requires_cooling,
        }
        for preset in SCENARIO_PRESETS.values()
    ]

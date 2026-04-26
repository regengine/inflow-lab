from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class ScenarioId(str, Enum):
    LEAFY_GREENS_SUPPLIER = "leafy_greens_supplier"
    FRESH_CUT_PROCESSOR = "fresh_cut_processor"
    RETAILER_READINESS_DEMO = "retailer_readiness_demo"


@dataclass(frozen=True, slots=True)
class Location:
    name: str
    location_type: str
    gln: str


@dataclass(frozen=True, slots=True)
class ProductSpec:
    name: str
    unit: str
    category: str


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
    harvest_target: int = 3
    packed_to_processor_probability: float = 0.45
    transform_input_choices: tuple[int, ...] = (2, 2, 3)

    @property
    def transform_min_lots(self) -> int:
        return min(self.transform_input_choices)


SCENARIO_PRESETS: dict[ScenarioId, ScenarioPreset] = {
    ScenarioId.LEAFY_GREENS_SUPPLIER: ScenarioPreset(
        id=ScenarioId.LEAFY_GREENS_SUPPLIER,
        label="Leafy greens supplier",
        description="Farm-origin leafy greens flowing through cooling, packout, and outbound cold-chain shipments.",
        farms=(
            Location("Valley Fresh Farms", "farm", "0850000001001"),
            Location("Desert Bloom Farm", "farm", "0850000001002"),
            Location("Riverbend Organics", "farm", "0850000001003"),
        ),
        coolers=(
            Location("Salinas Cooling Hub", "cooler", "0850000002001"),
            Location("Imperial Pre-Cool Facility", "cooler", "0850000002002"),
        ),
        packers=(
            Location("FreshPack Central", "packer", "0850000003001"),
            Location("GreenLeaf Packing House", "packer", "0850000003002"),
        ),
        processors=(
            Location("ReadyFresh Processing Plant", "processor", "0850000004001"),
            Location("DeliMix Plant", "processor", "0850000004002"),
        ),
        dcs=(
            Location("Distribution Center #4", "dc", "0850000005001"),
            Location("Distribution Center #7", "dc", "0850000005002"),
        ),
        retailers=(
            Location("Retail Store #4521", "retail", "0850000006001"),
            Location("Retail Store #3189", "retail", "0850000006002"),
        ),
        products=(
            ProductSpec("Romaine Lettuce", "cases", "leafy_greens"),
            ProductSpec("Green Leaf Lettuce", "cases", "leafy_greens"),
            ProductSpec("Spinach", "cases", "leafy_greens"),
            ProductSpec("Spring Mix", "cases", "leafy_greens"),
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
        harvest_target=3,
        packed_to_processor_probability=0.45,
    ),
    ScenarioId.FRESH_CUT_PROCESSOR: ScenarioPreset(
        id=ScenarioId.FRESH_CUT_PROCESSOR,
        label="Fresh-cut processor",
        description="Ingredient lots routed into processor inventory, transformed into fresh-cut outputs, then shipped onward.",
        farms=(
            Location("Valley Fresh Farms", "farm", "0850000001001"),
            Location("Coastal Leaf Farm", "farm", "0850000001011"),
            Location("Desert Bloom Farm", "farm", "0850000001002"),
        ),
        coolers=(
            Location("Salinas Cooling Hub", "cooler", "0850000002001"),
            Location("Coastal Cold Chain", "cooler", "0850000002011"),
        ),
        packers=(
            Location("Processor Intake Packout", "packer", "0850000003011"),
            Location("GreenLeaf Packing House", "packer", "0850000003002"),
        ),
        processors=(
            Location("ReadyFresh Processing Plant", "processor", "0850000004001"),
            Location("Urban Greens Fresh-Cut", "processor", "0850000004011"),
        ),
        dcs=(
            Location("Foodservice DC #12", "dc", "0850000005011"),
            Location("Regional Cold DC #3", "dc", "0850000005012"),
        ),
        retailers=(
            Location("Cafe Commissary #88", "retail", "0850000006011"),
            Location("Retail Store #4521", "retail", "0850000006001"),
        ),
        products=(
            ProductSpec("Romaine Lettuce", "cases", "leafy_greens"),
            ProductSpec("Spinach", "cases", "leafy_greens"),
            ProductSpec("Green Cabbage", "cases", "produce"),
            ProductSpec("Shredding Carrots", "cases", "produce"),
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
        harvest_target=4,
        packed_to_processor_probability=0.85,
        transform_input_choices=(2, 2, 3),
    ),
    ScenarioId.RETAILER_READINESS_DEMO: ScenarioPreset(
        id=ScenarioId.RETAILER_READINESS_DEMO,
        label="Retailer readiness demo",
        description="Retail-ready produce flows quickly through DC receiving and store-level downstream receipts.",
        farms=(
            Location("SunCoast Produce Ranch", "farm", "0850000001021"),
            Location("Valley Fresh Farms", "farm", "0850000001001"),
            Location("Riverbend Organics", "farm", "0850000001003"),
        ),
        coolers=(
            Location("Retail Cold Dock West", "cooler", "0850000002021"),
            Location("Salinas Cooling Hub", "cooler", "0850000002001"),
        ),
        packers=(
            Location("Retail Ready Packout", "packer", "0850000003021"),
            Location("FreshPack Central", "packer", "0850000003001"),
        ),
        processors=(
            Location("ReadyFresh Processing Plant", "processor", "0850000004001"),
            Location("Meal Kit Prep Center", "processor", "0850000004021"),
        ),
        dcs=(
            Location("Retail DC West", "dc", "0850000005021"),
            Location("Retail DC East", "dc", "0850000005022"),
        ),
        retailers=(
            Location("Retail Store #4521", "retail", "0850000006001"),
            Location("Retail Store #3189", "retail", "0850000006002"),
            Location("Urban Market #22", "retail", "0850000006021"),
        ),
        products=(
            ProductSpec("Romaine Hearts Retail Pack", "cases", "retail_ready"),
            ProductSpec("Baby Spinach Clamshells", "cases", "retail_ready"),
            ProductSpec("Spring Mix Clamshells", "cases", "retail_ready"),
            ProductSpec("Cucumber Snack Packs", "cases", "retail_ready"),
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
        harvest_target=3,
        packed_to_processor_probability=0.1,
        transform_input_choices=(2, 2),
    ),
}


def get_scenario(scenario_id: ScenarioId | str | None = None) -> ScenarioPreset:
    normalized = ScenarioId(scenario_id or ScenarioId.LEAFY_GREENS_SUPPLIER)
    return SCENARIO_PRESETS[normalized]


def list_scenario_summaries() -> list[dict[str, str]]:
    return [
        {
            "id": preset.id.value,
            "label": preset.label,
            "description": preset.description,
        }
        for preset in SCENARIO_PRESETS.values()
    ]

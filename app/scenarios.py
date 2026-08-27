from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import count
from typing import Mapping

from .schemas.domain import OperationScale


class ScenarioId(str, Enum):
    LEAFY_GREENS_SUPPLIER = "leafy_greens_supplier"
    FRESH_CUT_PROCESSOR = "fresh_cut_processor"
    RETAILER_READINESS_DEMO = "retailer_readiness_demo"
    SEAFOOD_FIRST_RECEIVER = "seafood_first_receiver"
    DAIRY_CONTINUOUS_FLOW = "dairy_continuous_flow"
    COPACKER_NUT_BUTTER = "copacker_nut_butter"
    BROADLINE_DISTRIBUTOR = "broadline_distributor"
    FOODSERVICE_RESTAURANT_GROUP = "foodservice_restaurant_group"
    SHELL_EGG_PRODUCER = "shell_egg_producer"


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
    # source_cte_type deliberately absent (#164): an operation's source CTE
    # type is a property of its *industry*, not of an individual scenario, and
    # it lives on IndustryAdapter -- alongside source_reference_type, which the
    # engine already reads from there. It used to be duplicated here as well,
    # with engine.py reading this copy and nothing at all reading the adapter's,
    # so the two had to be hand-synced across unrelated files and a new
    # industry/scenario pairing that updated only one would have diverged
    # silently. The engine now reads the adapter, which it derives from
    # industry_type, so the pairing cannot be inconsistent.
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


# Lot-volume multiplier per operation scale, applied to the industry
# adapter's source quantity range. A small producer harvests quarter-size
# lots; an enterprise harvests 4x lots.
SCALE_QUANTITY_MULTIPLIER: dict[OperationScale, float] = {
    OperationScale.SMALL: 0.25,
    OperationScale.MIDSIZE: 1.0,
    OperationScale.ENTERPRISE: 4.0,
}

# Facility-count targets per tier for the enterprise scale. Tiers that a
# preset leaves empty (e.g. farms for seafood first-receiver) stay empty.
_ENTERPRISE_TIER_TARGETS = {
    "farms": 12,
    "coolers": 4,
    "packers": 4,
    "processors": 3,
    "dcs": 5,
    "retailers": 10,
}

# Generated-location GLN ids start here so they never collide with the
# hand-authored preset ids (1001+).
_GENERATED_GLN_BASE = 60000


def scale_scenario(preset: ScenarioPreset, scale: OperationScale) -> ScenarioPreset:
    """Return a copy of the preset sized for the requested operation scale.

    - ``midsize`` is the preset as authored (the historical behavior).
    - ``small`` trims each facility tier to a single site (two retail
      destinations), halves the lots-in-flight target, keeps most product
      moving direct rather than into processing, and de-emphasizes
      transformation.
    - ``enterprise`` expands each non-empty tier to a multi-site network
      (cloned sites with fresh, valid GLNs) and quadruples the
      lots-in-flight target so commingling and multi-DC routing dominate.

    Deterministic: no RNG — the same preset and scale always produce the
    same network, so seeded runs stay reproducible.
    """
    if scale is OperationScale.MIDSIZE:
        return preset

    if scale is OperationScale.SMALL:
        weights = dict(preset.action_weights)
        if weights.get("transform"):
            weights["transform"] = max(1, weights["transform"] // 2)
        return replace(
            preset,
            farms=preset.farms[:1],
            coolers=preset.coolers[:1],
            packers=preset.packers[:1],
            processors=preset.processors[:1],
            dcs=preset.dcs[:1],
            retailers=preset.retailers[:2],
            harvest_target=max(1, preset.harvest_target // 2),
            packed_to_processor_probability=min(preset.packed_to_processor_probability, 0.2),
            action_weights=weights,
        )

    gln_ids = count(_GENERATED_GLN_BASE)

    def expand(locations: tuple[Location, ...], target: int) -> tuple[Location, ...]:
        if not locations:
            return locations
        expanded = list(locations)
        site_number = 2
        while len(expanded) < target:
            base = locations[len(expanded) % len(locations)]
            expanded.append(
                Location(
                    f"{base.name} — Site {site_number}",
                    base.location_type,
                    _gln(next(gln_ids)),
                    dict(base.metadata),
                )
            )
            site_number += 1
        return tuple(expanded)

    return replace(
        preset,
        farms=expand(preset.farms, _ENTERPRISE_TIER_TARGETS["farms"]),
        coolers=expand(preset.coolers, _ENTERPRISE_TIER_TARGETS["coolers"]),
        packers=expand(preset.packers, _ENTERPRISE_TIER_TARGETS["packers"]),
        processors=expand(preset.processors, _ENTERPRISE_TIER_TARGETS["processors"]),
        dcs=expand(preset.dcs, _ENTERPRISE_TIER_TARGETS["dcs"]),
        retailers=expand(preset.retailers, _ENTERPRISE_TIER_TARGETS["retailers"]),
        harvest_target=preset.harvest_target * 4,
    )


# ---------------------------------------------------------------------------
# Additional FSMA 204 covered-entity personas. Subpart S applies to anyone
# who manufactures, processes, packs, or holds foods on the Food
# Traceability List — these presets round out the entity types beyond the
# original farm / fresh-cut / retail / seafood / dairy set. All flow
# through the same CTE engine; only the network, products, and flow mix
# change, so every event stays RegEngine-canonical.
# ---------------------------------------------------------------------------

SCENARIO_PRESETS[ScenarioId.COPACKER_NUT_BUTTER] = ScenarioPreset(
    id=ScenarioId.COPACKER_NUT_BUTTER,
    label="Co-packer (nut butters)",
    description="Contract manufacturer receiving ingredient nut lots from grower partners and transforming them into branded and private-label nut butters (FTL: nut butters).",
    farms=(
        Location("Central Valley Almond Ranch", "farm", _gln(1141), {"gps": "36.7378,-119.7871"}),
        Location("Georgia Grove Peanut Farm", "farm", _gln(1142), {"gps": "31.5785,-84.1557"}),
        Location("Sunland Pecan Orchards", "farm", _gln(1143), {"gps": "32.3199,-106.7637"}),
    ),
    coolers=(
        Location("Hulling & Conditioning Station", "cooler", _gln(2141)),
    ),
    packers=(
        Location("Ingredient Intake Dock", "packer", _gln(3141)),
        Location("Roastline Staging", "packer", _gln(3142)),
    ),
    processors=(
        Location("MillRight Contract Manufacturing — Line 1", "processor", _gln(4141)),
        Location("MillRight Contract Manufacturing — Line 2", "processor", _gln(4142)),
    ),
    dcs=(
        Location("Brand Fulfillment DC", "dc", _gln(5141)),
        Location("Private Label DC East", "dc", _gln(5142)),
    ),
    retailers=(
        Location("Retail Store #2210", "retail", _gln(6141)),
        Location("Grocer Chain DC Receipt", "retail", _gln(6142)),
    ),
    products=(
        ProductSpec("Raw Almonds", "totes", "tree_nuts", {"plu": "3339"}),
        ProductSpec("Blanched Peanuts", "totes", "peanuts", {"plu": "3338"}),
        ProductSpec("Pecan Halves", "totes", "tree_nuts", {"plu": "3341"}),
    ),
    transformation_outputs=(
        "Brand A Creamy Almond Butter",
        "Private Label Crunchy Peanut Butter",
        "Foodservice Pecan Spread",
        "Brand B Honey Peanut Butter",
    ),
    carriers=("BulkHaul Ingredients", "SwiftChain Logistics", "ColdRoute Freight"),
    action_weights={
        "harvest": 4,
        "cool": 3,
        "initial_pack": 5,
        "ship": 5,
        "receive": 6,
        "transform": 9,
    },
    industry_type="produce",
    operation_type="copacker",
    reference_format="GS1",
    harvest_target=4,
    packed_to_processor_probability=0.95,
    transform_input_choices=(2, 3, 3),
)

SCENARIO_PRESETS[ScenarioId.BROADLINE_DISTRIBUTOR] = ScenarioPreset(
    id=ScenarioId.BROADLINE_DISTRIBUTOR,
    label="Broadline distributor",
    description="Wholesale distributor holding and moving FTL produce between grower-shippers and retail/foodservice customers — pure ship/receive traceability with no transformation.",
    farms=(
        Location("Grower-Shipper Alliance #1", "farm", _gln(1111), {"gps": "36.6777,-121.6555"}),
        Location("Grower-Shipper Alliance #2", "farm", _gln(1112), {"gps": "33.0581,-112.0476"}),
    ),
    coolers=(
        Location("Consolidation Pre-Cool", "cooler", _gln(2111)),
    ),
    packers=(
        Location("Repack & Consolidation Center", "packer", _gln(3111)),
    ),
    processors=(),
    dcs=(
        Location("Broadline DC North", "dc", _gln(5111)),
        Location("Broadline DC South", "dc", _gln(5112)),
        Location("Cross-Dock Hub", "dc", _gln(5113)),
    ),
    retailers=(
        Location("Independent Grocer #77", "retail", _gln(6111)),
        Location("Restaurant Supply Pickup Dock", "retail", _gln(6112)),
        Location("Regional Chain Store #402", "retail", _gln(6113)),
    ),
    products=(
        ProductSpec("Romaine Lettuce", "cases", "leafy_greens", {"plu": "4640"}),
        ProductSpec("Roma Tomatoes", "cases", "tomatoes", {"plu": "4087"}),
        ProductSpec("English Cucumbers", "cases", "cucumbers", {"plu": "4593"}),
        ProductSpec("Cantaloupe", "cases", "melons", {"plu": "4050"}),
    ),
    transformation_outputs=(),
    carriers=("Broadline Fleet 12", "Broadline Fleet 18", "SwiftChain Logistics"),
    action_weights={
        "harvest": 4,
        "cool": 3,
        "initial_pack": 4,
        "ship": 8,
        "receive": 8,
        "transform": 0,
    },
    industry_type="produce",
    operation_type="distributor",
    reference_format="GS1",
    harvest_target=4,
    packed_to_processor_probability=0.0,
)

SCENARIO_PRESETS[ScenarioId.FOODSERVICE_RESTAURANT_GROUP] = ScenarioPreset(
    id=ScenarioId.FOODSERVICE_RESTAURANT_GROUP,
    label="Foodservice restaurant group",
    description="Restaurant group receiving FTL produce through a central commissary that preps ready-to-eat deli salads (FTL: fresh RTE deli salads) for its locations.",
    farms=(
        Location("Farm Direct Partner — Salinas", "farm", _gln(1121), {"gps": "36.6777,-121.6555"}),
        Location("Farm Direct Partner — Yuma", "farm", _gln(1122), {"gps": "32.6927,-114.6277"}),
    ),
    coolers=(
        Location("Commissary Cold Intake", "cooler", _gln(2121)),
    ),
    packers=(
        Location("Commissary Prep Packout", "packer", _gln(3121)),
    ),
    processors=(
        Location("Central Commissary Kitchen", "processor", _gln(4121)),
    ),
    dcs=(
        Location("Restaurant Distribution Hub", "dc", _gln(5121)),
    ),
    retailers=(
        Location("Harvest Table Bistro — Downtown", "retail", _gln(6121)),
        Location("Harvest Table Bistro — Airport", "retail", _gln(6122)),
        Location("Harvest Table Bistro — University", "retail", _gln(6123)),
    ),
    products=(
        ProductSpec("Romaine Lettuce", "cases", "leafy_greens", {"plu": "4640"}),
        ProductSpec("Cherry Tomatoes", "flats", "tomatoes", {"plu": "4796"}),
        ProductSpec("Basil", "cases", "herbs", {"plu": "4885"}),
    ),
    transformation_outputs=(
        "House Caesar Salad (RTE)",
        "Caprese Deli Salad (RTE)",
        "Seasonal Greens Bowl Base",
    ),
    carriers=("Commissary Van Fleet", "ColdRoute Freight"),
    action_weights={
        "harvest": 4,
        "cool": 4,
        "initial_pack": 4,
        "ship": 5,
        "receive": 6,
        "transform": 5,
    },
    industry_type="produce",
    operation_type="foodservice",
    reference_format="PLAIN",
    harvest_target=3,
    packed_to_processor_probability=0.7,
)

SCENARIO_PRESETS[ScenarioId.SHELL_EGG_PRODUCER] = ScenarioPreset(
    id=ScenarioId.SHELL_EGG_PRODUCER,
    label="Shell egg producer-packer",
    description="Layer operation cooling, grading, and packing shell eggs (FTL: shell eggs), then shipping cartons to DCs and retail — no transformation, egg products are out of scope.",
    farms=(
        Location("Sunrise Layer House A", "farm", _gln(1131), {"gps": "40.7960,-91.1129"}),
        Location("Sunrise Layer House B", "farm", _gln(1132), {"gps": "40.8014,-91.1204"}),
    ),
    coolers=(
        Location("Egg Cooling Room 1", "cooler", _gln(2131)),
    ),
    packers=(
        Location("Grading & Carton Packing Line", "packer", _gln(3131)),
    ),
    processors=(),
    dcs=(
        Location("Regional Grocery DC", "dc", _gln(5131)),
        Location("Club Store DC", "dc", _gln(5132)),
    ),
    retailers=(
        Location("Retail Store #1180", "retail", _gln(6131)),
        Location("Club Store #44", "retail", _gln(6132)),
    ),
    products=(
        ProductSpec("Large Grade A Shell Eggs", "cases", "shell_eggs", {"plu": "9748"}),
        ProductSpec("Organic Brown Shell Eggs", "cases", "shell_eggs", {"plu": "9749"}),
    ),
    transformation_outputs=(),
    carriers=("FarmFresh Refrigerated", "SwiftChain Logistics"),
    action_weights={
        "harvest": 6,
        "cool": 5,
        "initial_pack": 5,
        "ship": 5,
        "receive": 5,
        "transform": 0,
    },
    industry_type="produce",
    operation_type="egg_producer",
    reference_format="PLAIN",
    harvest_target=3,
    packed_to_processor_probability=0.0,
)

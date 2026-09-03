from collections import Counter

from app.engine import LegitFlowEngine
<<<<<<< HEAD
from app.scenarios import ScenarioId, get_scenario, list_scenario_summaries
from app.schemas.domain import CTEType
=======
from app.schemas.domain import CTEType
from app.industry_adapters import IndustryAdapter, get_industry_adapter
from app.scenarios import SCENARIO_PRESETS, ScenarioId, get_scenario, list_scenario_summaries
>>>>>>> origin/main


def event_signature(scenario: ScenarioId) -> list[tuple[str, str, str, tuple[str, ...]]]:
    engine = LegitFlowEngine(seed=204, scenario=scenario)
    signature = []
    for _ in range(40):
        event, parents = engine.next_event()
        signature.append(
            (
                event.cte_type.value,
                event.product_description,
                event.location_name,
                tuple(parents),
            )
        )
    return signature


def test_scenario_catalog_lists_required_presets():
    summaries = list_scenario_summaries()

    assert [summary["id"] for summary in summaries] == [
        "leafy_greens_supplier",
        "fresh_cut_processor",
        "retailer_readiness_demo",
        "seafood_first_receiver",
        "dairy_continuous_flow",
        "copacker_nut_butter",
        "broadline_distributor",
        "foodservice_restaurant_group",
        "shell_egg_producer",
    ]
    assert all(summary["label"] for summary in summaries)
    assert all(summary["description"] for summary in summaries)
    assert {summary["industry_type"] for summary in summaries} >= {"produce", "seafood", "dairy"}
    assert {summary["operation_type"] for summary in summaries} >= {"supplier", "processor", "retailer", "first_receiver"}
    assert all(summary["reference_format"] for summary in summaries)


def test_scenario_generation_is_deterministic_for_seed():
    assert event_signature(ScenarioId.FRESH_CUT_PROCESSOR) == event_signature(ScenarioId.FRESH_CUT_PROCESSOR)
    assert event_signature(ScenarioId.FRESH_CUT_PROCESSOR) != event_signature(ScenarioId.RETAILER_READINESS_DEMO)


def test_scenario_presets_produce_distinct_product_and_flow_mixes():
    events_by_scenario = {}
    for scenario in ScenarioId:
        engine = LegitFlowEngine(seed=204, scenario=scenario)
        events_by_scenario[scenario] = [engine.next_event()[0] for _ in range(160)]

    leafy_products = {product.name for product in get_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER).products}
    retailer_products = {product.name for product in get_scenario(ScenarioId.RETAILER_READINESS_DEMO).products}
    fresh_cut_outputs = set(get_scenario(ScenarioId.FRESH_CUT_PROCESSOR).transformation_outputs)
    seafood_products = {product.name for product in get_scenario(ScenarioId.SEAFOOD_FIRST_RECEIVER).products}

    leafy_harvests = {
        event.product_description
        for event in events_by_scenario[ScenarioId.LEAFY_GREENS_SUPPLIER]
        if event.cte_type == CTEType.HARVESTING
    }
    retailer_harvests = {
        event.product_description
        for event in events_by_scenario[ScenarioId.RETAILER_READINESS_DEMO]
        if event.cte_type == CTEType.HARVESTING
    }
    fresh_cut_products = {event.product_description for event in events_by_scenario[ScenarioId.FRESH_CUT_PROCESSOR]}
    fresh_cut_counts = Counter(event.cte_type for event in events_by_scenario[ScenarioId.FRESH_CUT_PROCESSOR])
    retailer_locations = {event.location_name for event in events_by_scenario[ScenarioId.RETAILER_READINESS_DEMO]}
    seafood_ctes = {event.cte_type for event in events_by_scenario[ScenarioId.SEAFOOD_FIRST_RECEIVER]}
    seafood_seen_products = {
        event.product_description
        for event in events_by_scenario[ScenarioId.SEAFOOD_FIRST_RECEIVER]
        if event.cte_type == CTEType.FIRST_LAND_BASED_RECEIVING
    }
    dairy_harvest_units = {
        event.unit_of_measure
        for event in events_by_scenario[ScenarioId.DAIRY_CONTINUOUS_FLOW]
        if event.cte_type == CTEType.HARVESTING
    }

    assert leafy_harvests <= leafy_products
    assert retailer_harvests <= retailer_products
    assert leafy_harvests.isdisjoint(retailer_harvests)
    assert fresh_cut_products & fresh_cut_outputs
    assert fresh_cut_counts[CTEType.TRANSFORMATION] > 0
    assert {"Retail DC West", "Retail Store #4521"} <= retailer_locations
    assert CTEType.FIRST_LAND_BASED_RECEIVING in seafood_ctes
    assert seafood_seen_products <= seafood_products
    assert dairy_harvest_units == {"gallons"}


# ---------------------------------------------------------------------------
# #164 — one source of truth for an adapter's source CTE type.
# ---------------------------------------------------------------------------


def test_source_cte_type_lives_only_on_the_industry_adapter():
    """``ScenarioPreset`` used to carry its own ``source_cte_type`` that the
    engine read, while ``IndustryAdapter.source_cte_type`` -- the one a reader
    of industry_adapters.py would expect to matter -- was never read by
    anything. Two hand-synced copies in unrelated files. The adapter is now
    the only definition, so a preset cannot reintroduce a second one.
    """
    assert "source_cte_type" in IndustryAdapter.__dataclass_fields__
    for scenario_id, preset in SCENARIO_PRESETS.items():
        assert not hasattr(preset, "source_cte_type"), scenario_id


def test_the_engine_takes_its_source_cte_type_from_the_active_adapter():
    """Changing the adapter's value must change generated CTE behavior -- the
    property the old dead field falsely implied. Seafood is the only industry
    whose source CTE differs from the default, so it is the real-data case;
    the override below proves the wiring rather than the data.
    """
    seafood_events = _source_events(ScenarioId.SEAFOOD_FIRST_RECEIVER)
    assert seafood_events
    assert all(event.cte_type is CTEType.FIRST_LAND_BASED_RECEIVING for event in seafood_events)

    produce_events = _source_events(ScenarioId.LEAFY_GREENS_SUPPLIER)
    assert produce_events
    assert all(event.cte_type is CTEType.HARVESTING for event in produce_events)

    # The adapter genuinely drives it: swap the value the engine's adapter
    # reports and the emitted source events follow.
    engine = LegitFlowEngine(seed=204, scenario=ScenarioId.LEAFY_GREENS_SUPPLIER)
    produce = get_industry_adapter("produce")
    engine.adapter = IndustryAdapter(
        industry_type=produce.industry_type,
        source_cte_type=CTEType.FIRST_LAND_BASED_RECEIVING,
        source_reference_type=produce.source_reference_type,
    )
    swapped = [
        event
        for event in (engine.next_event()[0] for _ in range(40))
        if event.cte_type in {CTEType.HARVESTING, CTEType.FIRST_LAND_BASED_RECEIVING}
    ]
    assert swapped
    assert all(event.cte_type is CTEType.FIRST_LAND_BASED_RECEIVING for event in swapped)


def _source_events(scenario: ScenarioId):
    engine = LegitFlowEngine(seed=204, scenario=scenario)
    return [
        event
        for event in (engine.next_event()[0] for _ in range(40))
        if event.cte_type in {CTEType.HARVESTING, CTEType.FIRST_LAND_BASED_RECEIVING}
    ]

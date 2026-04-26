from collections import Counter

from app.engine import LegitFlowEngine
from app.models import CTEType
from app.scenarios import ScenarioId, get_scenario, list_scenario_summaries


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
    ]
    assert all(summary["label"] for summary in summaries)
    assert all(summary["description"] for summary in summaries)


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

    assert leafy_harvests <= leafy_products
    assert retailer_harvests <= retailer_products
    assert leafy_harvests.isdisjoint(retailer_harvests)
    assert fresh_cut_products & fresh_cut_outputs
    assert fresh_cut_counts[CTEType.TRANSFORMATION] > 0
    assert {"Retail DC West", "Retail Store #4521"} <= retailer_locations

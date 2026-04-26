from app.engine import LegitFlowEngine
from app.models import CTEType


def test_initial_packing_sources_previously_cooled_lots():
    engine = LegitFlowEngine(seed=204)
    cooled_lots = set()
    packing_seen = False

    for _ in range(80):
        event, parents = engine.next_event()

        if event.cte_type == CTEType.COOLING:
            cooled_lots.add(event.traceability_lot_code)
        elif event.cte_type == CTEType.INITIAL_PACKING:
            packing_seen = True
            source_lot_code = event.kdes["source_traceability_lot_code"]
            assert parents == [source_lot_code]
            assert source_lot_code in cooled_lots

    assert packing_seen


def test_harvest_immediate_subsequent_recipients_are_coolers():
    engine = LegitFlowEngine(seed=204)
    cooler_names = {cooler.name for cooler in engine.coolers}
    harvest_recipients = set()

    for _ in range(80):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.HARVESTING:
            harvest_recipients.add(event.kdes["immediate_subsequent_recipient"])

    assert harvest_recipients
    assert harvest_recipients <= cooler_names


def test_engine_emits_all_supported_ctes_and_lineage():
    engine = LegitFlowEngine(seed=204)
    expected_ctes = set(CTEType)
    seen = set()
    parent_links = 0

    for _ in range(160):
        event, parents = engine.next_event()
        seen.add(event.cte_type)
        if parents:
            parent_links += 1
        if seen == expected_ctes and parent_links > 0:
            break

    assert seen == expected_ctes
    assert parent_links > 0


def test_location_gln_lookup():
    engine = LegitFlowEngine(seed=204)
    assert engine.location_gln("Valley Fresh Farms") == "0850000001001"

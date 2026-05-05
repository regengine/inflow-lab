from datetime import UTC, datetime, timedelta

from app.engine import LegitFlowEngine
from app.schemas.domain import CTEType


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
    # CTEs the default LegitFlowEngine actively emits across the
    # leafy-greens / fresh-cut / retailer scenarios. CTEType also
    # includes FIRST_LAND_BASED_RECEIVING (21 CFR §1.1325) for
    # contract parity with RegEngine's webhook ingest, but it is
    # intentionally not part of this engine's flow until a seafood
    # scenario is added.
    expected_ctes = {
        CTEType.HARVESTING,
        CTEType.COOLING,
        CTEType.INITIAL_PACKING,
        CTEType.SHIPPING,
        CTEType.RECEIVING,
        CTEType.TRANSFORMATION,
    }
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


def test_engine_emits_regengine_canonical_kdes_for_lab_contract():
    engine = LegitFlowEngine(seed=204)
    seen = {}

    for _ in range(120):
        event, _ = engine.next_event()
        seen.setdefault(event.cte_type, event)

    harvesting = seen[CTEType.HARVESTING]
    assert "reference_document_type" in harvesting.kdes
    assert "reference_document_number" in harvesting.kdes
    # Required for RegEngine ingest contract — webhook_router_v2 KDE
    # validator looks for the combined `reference_document` field on
    # harvesting events, not just the split type/number fields.
    assert harvesting.kdes["reference_document"]

    initial_packing = seen[CTEType.INITIAL_PACKING]
    assert initial_packing.kdes["packing_date"]
    assert initial_packing.kdes["pack_date"]
    assert initial_packing.kdes["reference_document"]
    assert initial_packing.kdes["harvester_business_name"]

    shipping = seen[CTEType.SHIPPING]
    assert shipping.kdes["reference_document"]
    assert shipping.kdes["tlc_source_reference"]
    assert shipping.kdes["traceability_lot_code_source_reference"]


def test_engine_clock_stays_inside_live_webhook_window_for_demo_loop():
    engine = LegitFlowEngine(seed=204)
    timestamps = [engine.next_event()[0].timestamp for _ in range(300)]
    now = datetime.now(UTC)

    assert max(timestamps) <= now + timedelta(days=30)
    assert min(timestamps) >= now - timedelta(days=90)

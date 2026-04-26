from datetime import UTC, datetime, timedelta

from app.engine import LegitFlowEngine
from app.models import CTEType


def test_engine_emits_supported_ctes_and_lineage():
    engine = LegitFlowEngine(seed=204)
    seen = set()
    parent_links = 0
    for _ in range(80):
        event, parents = engine.next_event()
        seen.add(event.cte_type)
        if parents:
            parent_links += 1
    assert CTEType.HARVESTING in seen
    assert CTEType.SHIPPING in seen
    assert CTEType.RECEIVING in seen
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
    assert harvesting.kdes["reference_document"]
    assert "reference_document_type" in harvesting.kdes
    assert "reference_document_number" in harvesting.kdes

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

    assert max(timestamps) <= now + timedelta(hours=24)
    assert min(timestamps) >= now - timedelta(days=90)

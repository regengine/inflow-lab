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
    # Canonical RegEngine ingest contract uses the unified
    # `reference_document` field, not a split type/number pair.
    assert harvesting.kdes["reference_document"]

    initial_packing = seen[CTEType.INITIAL_PACKING]
    assert initial_packing.kdes["packing_date"]
    assert initial_packing.kdes["reference_document"]
    assert initial_packing.kdes["harvester_business_name"]

    shipping = seen[CTEType.SHIPPING]
    assert shipping.kdes["reference_document"]
    assert shipping.kdes["tlc_source_reference"]


# Hardcoded copy of RegEngine's REQUIRED_KDES_BY_CTE
# (services/ingestion/app/webhook_models.py). Pinned here intentionally
# — DO NOT replace with an import from RegEngine. Drift between this
# constant and the engine's emitted KDEs should fail CI loudly so we
# catch wire-compat regressions before they reach a tenant.
REGENGINE_REQUIRED_KDES: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "harvest_date",
        "location_name",
        "reference_document",
    ),
    CTEType.COOLING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "cooling_date",
        "location_name",
        "reference_document",
    ),
    CTEType.INITIAL_PACKING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "packing_date",
        "location_name",
        "reference_document",
        "harvester_business_name",
    ),
    CTEType.SHIPPING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "ship_date",
        "ship_from_location",
        "ship_to_location",
        "reference_document",
        "tlc_source_reference",
    ),
    CTEType.RECEIVING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "receive_date",
        "receiving_location",
        "immediate_previous_source",
        "reference_document",
        "tlc_source_reference",
    ),
    CTEType.TRANSFORMATION: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "transformation_date",
        "location_name",
        "reference_document",
    ),
}


def test_emitted_events_satisfy_regengine_required_kdes():
    """Every event the engine emits must satisfy RegEngine's contract.

    The RegEngine validator merges top-level IngestEvent fields with the
    kdes dict before checking required keys, so we replicate that merge
    here.
    """
    engine = LegitFlowEngine(seed=204)
    seen_ctes: set[CTEType] = set()

    for _ in range(400):
        event, _ = engine.next_event()
        required = REGENGINE_REQUIRED_KDES.get(event.cte_type)
        if required is None:
            continue
        seen_ctes.add(event.cte_type)
        available = {
            "traceability_lot_code": event.traceability_lot_code,
            "product_description": event.product_description,
            "quantity": event.quantity,
            "unit_of_measure": event.unit_of_measure,
            "location_name": event.location_name,
            **event.kdes,
        }
        missing = [field for field in required if not _has_kde_value(available.get(field))]
        assert not missing, (
            f"{event.cte_type.value} event missing required KDEs {missing}; "
            f"available keys: {sorted(available.keys())}"
        )

    assert seen_ctes == set(REGENGINE_REQUIRED_KDES.keys())


def test_emitted_events_carry_location_gln_for_known_locations():
    """Every event for a known scenario location must emit location_gln.

    The simulator's locations all have GLNs configured in scenarios.py,
    so every event the engine produces should carry a non-None
    location_gln populated from engine.location_gln(name).
    """
    engine = LegitFlowEngine(seed=204)
    seen_ctes: set[CTEType] = set()

    for _ in range(200):
        event, _ = engine.next_event()
        seen_ctes.add(event.cte_type)
        # location_name should always resolve in the scenario index, so
        # location_gln must be populated and match the engine lookup.
        assert event.location_gln is not None
        assert event.location_gln == engine.location_gln(event.location_name)

    # We exercised every CTE type the engine actively emits.
    assert {
        CTEType.HARVESTING,
        CTEType.COOLING,
        CTEType.INITIAL_PACKING,
        CTEType.SHIPPING,
        CTEType.RECEIVING,
        CTEType.TRANSFORMATION,
    } <= seen_ctes


def test_location_gln_or_none_returns_none_for_unknown_location():
    engine = LegitFlowEngine(seed=204)
    assert engine._location_gln_or_none("Unknown Place") is None


def test_regengine_event_back_compat_loads_old_jsonl_without_location_gln():
    """Old persisted events lack location_gln; field is Optional so they
    must still deserialize cleanly with location_gln=None.
    """
    from app.schemas.domain import RegEngineEvent

    legacy_payload = (
        '{"cte_type":"harvesting","traceability_lot_code":"TLC-1",'
        '"product_description":"Romaine","quantity":100.0,'
        '"unit_of_measure":"cases","location_name":"Valley Fresh Farms",'
        '"timestamp":"2026-01-01T00:00:00Z","kdes":{}}'
    )
    event = RegEngineEvent.model_validate_json(legacy_payload)
    assert event.location_gln is None


def _has_kde_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def test_engine_clock_stays_inside_live_webhook_window_for_demo_loop():
    engine = LegitFlowEngine(seed=204)
    timestamps = [engine.next_event()[0].timestamp for _ in range(300)]
    now = datetime.now(UTC)

    assert max(timestamps) <= now + timedelta(hours=20, seconds=1)
    assert min(timestamps) >= now - timedelta(days=90)


def test_seafood_scenario_emits_first_land_based_receiving_kdes():
    engine = LegitFlowEngine(seed=204, scenario="seafood_first_receiver")

    for _ in range(60):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.FIRST_LAND_BASED_RECEIVING:
            assert event.kdes["landing_date"]
            assert event.kdes["vessel_identifier"]
            assert event.kdes["first_land_based_receiver"] == event.location_name
            assert event.kdes["reference_document"].startswith("GS1-128 (00)")
            return

    raise AssertionError("Expected a first land-based receiving event")


def test_shipping_and_receiving_share_same_gs1_reference_document():
    engine = LegitFlowEngine(seed=204, scenario="seafood_first_receiver")
    shipping_document = None
    shipping_number = None

    for _ in range(120):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.SHIPPING:
            shipping_document = event.kdes["reference_document"]
            shipping_number = event.kdes["reference_document_number"]
        elif event.cte_type == CTEType.RECEIVING and shipping_document:
            assert event.kdes["reference_document"] == shipping_document
            assert event.kdes["reference_document_number"] == shipping_number
            assert event.kdes["sscc"] == shipping_number
            return

    raise AssertionError("Expected linked shipping and receiving events")


def test_dairy_scenario_supports_continuous_flow_without_cooling_step():
    engine = LegitFlowEngine(seed=204, scenario="dairy_continuous_flow")

    for _ in range(80):
        event, _ = engine.next_event()
        assert event.cte_type != CTEType.COOLING
        if event.cte_type == CTEType.INITIAL_PACKING:
            assert event.kdes["flow_type"] == "continuous"
            assert "silo_identifier" in event.kdes
            return

    raise AssertionError("Expected a dairy initial packing event")


def test_transformation_can_emit_split_outputs_and_rework():
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")

    for _ in range(180):
        event, _ = engine.next_event()
        if event.cte_type == CTEType.TRANSFORMATION:
            assert len(event.kdes["input_traceability_lot_codes"]) >= 2
            assert event.kdes["output_traceability_lot_codes"]
            assert event.kdes["commingled_input_lot_count"] >= 2
            assert "lineage_pattern" in event.kdes
            return

    raise AssertionError("Expected a transformation event")

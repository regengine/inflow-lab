"""Pins the produce-importer persona's FSMA 204 semantics.

FSMA 204 has no "import" CTE: an importer's first domestic traceability
event is a §1.1345 receiving from the foreign shipper. First land-based
receiving (§1.1325(c)) is reserved for food obtained from fishing
vessels, and the foreign harvest must never be re-attributed to the
domestic import facility.
"""

from __future__ import annotations

from app.engine import LegitFlowEngine
from app.mock_service import validate_event_like_regengine
from app.scenarios import ScenarioId, get_scenario
from app.schemas.domain import CTEType, OperationScale


def _events(scale: OperationScale = OperationScale.MIDSIZE, n: int = 200):
    engine = LegitFlowEngine()
    engine.reset(204, scenario=ScenarioId.PRODUCE_IMPORTER, scale=scale)
    return [engine.next_event()[0] for _ in range(n)]


def test_importer_source_events_are_receiving_not_harvest_or_flbr() -> None:
    preset = get_scenario(ScenarioId.PRODUCE_IMPORTER)
    assert preset.source_cte_type is CTEType.RECEIVING
    events = _events()
    emitted = {event.cte_type for event in events}
    assert CTEType.RECEIVING in emitted
    assert CTEType.HARVESTING not in emitted
    assert CTEType.FIRST_LAND_BASED_RECEIVING not in emitted
    # Importer-distributor: no cooling CTE (post-entry cold storage is not a
    # §1.1325(b) cooling event) and no transformation.
    assert CTEType.COOLING not in emitted
    assert CTEType.TRANSFORMATION not in emitted


def test_importer_receipts_carry_foreign_source_and_customs_context() -> None:
    receipts = [
        event
        for event in _events()
        if event.cte_type is CTEType.RECEIVING and "customs_entry_number" in event.kdes
    ]
    assert receipts, "expected port-of-entry source receipts"
    for event in receipts:
        kdes = event.kdes
        assert kdes["immediate_previous_source"] == kdes["foreign_supplier_name"]
        assert kdes["country_of_origin"] in {"MX", "PE", "GT"}
        assert kdes["customs_entry_number"].startswith("ENTRY-")
        assert kdes["fsvp_importer_of_record"]
        assert "Customs Entry" in kdes["reference_document"]
        # Foreign harvest context travels with the receipt for later repack.
        assert kdes["harvest_date"] < kdes["receive_date"]
        assert f"({kdes['country_of_origin']})" in kdes["harvest_location_name"]


def test_importer_repack_attributes_harvest_to_foreign_grower_not_import_dock() -> None:
    preset = get_scenario(ScenarioId.PRODUCE_IMPORTER)
    import_facilities = {location.name for location in preset.farms}
    packs = [event for event in _events() if event.cte_type is CTEType.INITIAL_PACKING]
    assert packs, "expected repack events"
    for event in packs:
        kdes = event.kdes
        assert kdes["harvest_location_name"] not in import_facilities
        assert kdes["harvester_business_name"] not in import_facilities
        assert kdes["country_of_origin"] in {"MX", "PE", "GT"}
        assert kdes["customs_entry_number"].startswith("ENTRY-")


def test_importer_stays_regengine_canonical_at_every_scale() -> None:
    for scale in OperationScale:
        for event in _events(scale):
            assert validate_event_like_regengine(event) == [], (scale, event.cte_type)

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.engine import LegitFlowEngine
from app.main import app
from app.tenancy import controller
from app.mock_service import validate_event_like_regengine
from app.scenarios import SCENARIO_PRESETS, ScenarioId, get_scenario, scale_scenario
from app.schemas.domain import OperationScale
from app.schemas.simulation import SimulationConfig


client = TestClient(app)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def _gln_check_digit_valid(gln: str) -> bool:
    if len(gln) != 13 or not gln.isdigit():
        return False
    total = sum(
        int(digit) * (1 if index % 2 else 3)
        for index, digit in enumerate(reversed(gln[:-1]))
    )
    return (10 - (total % 10)) % 10 == int(gln[-1])


def test_midsize_is_the_preset_unchanged() -> None:
    for preset in SCENARIO_PRESETS.values():
        assert scale_scenario(preset, OperationScale.MIDSIZE) is preset


def test_small_trims_network_and_enterprise_expands_it() -> None:
    for preset in SCENARIO_PRESETS.values():
        small = scale_scenario(preset, OperationScale.SMALL)
        enterprise = scale_scenario(preset, OperationScale.ENTERPRISE)
        for tier in ("farms", "coolers", "packers", "processors", "dcs", "retailers"):
            base = getattr(preset, tier)
            if not base:
                # Empty tiers (e.g. seafood has no farms) stay empty at every scale.
                assert getattr(small, tier) == ()
                assert getattr(enterprise, tier) == ()
                continue
            assert len(getattr(small, tier)) <= len(base)
            assert len(getattr(enterprise, tier)) >= len(base)
        assert small.harvest_target < enterprise.harvest_target
        assert len(small.farms or small.coolers) == 1


def test_enterprise_generated_locations_have_valid_unique_glns() -> None:
    preset = get_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER)
    enterprise = scale_scenario(preset, OperationScale.ENTERPRISE)
    all_locations = [
        *enterprise.farms,
        *enterprise.coolers,
        *enterprise.packers,
        *enterprise.processors,
        *enterprise.dcs,
        *enterprise.retailers,
    ]
    glns = [loc.gln for loc in all_locations]
    names = [loc.name for loc in all_locations]
    assert len(set(glns)) == len(glns), "generated GLNs must not collide"
    assert len(set(names)) == len(names), "generated site names must be unique"
    for gln in glns:
        assert _gln_check_digit_valid(gln), gln


def test_scaled_events_stay_regengine_canonical() -> None:
    for scale in OperationScale:
        for scenario_id in ScenarioId:
            engine = LegitFlowEngine()
            engine.reset(204, scenario=scenario_id, scale=scale)
            for _ in range(120):
                event, _parents = engine.next_event()
                assert validate_event_like_regengine(event) == [], (scale, scenario_id, event.cte_type)


def test_scale_shifts_lot_volumes() -> None:
    def max_source_quantity(scale: OperationScale) -> float:
        engine = LegitFlowEngine()
        engine.reset(204, scenario=ScenarioId.LEAFY_GREENS_SUPPLIER, scale=scale)
        quantities = []
        for _ in range(60):
            event, _ = engine.next_event()
            if event.cte_type.value == "harvesting":
                quantities.append(event.quantity)
        return max(quantities)

    small = max_source_quantity(OperationScale.SMALL)
    midsize = max_source_quantity(OperationScale.MIDSIZE)
    enterprise = max_source_quantity(OperationScale.ENTERPRISE)
    assert small < midsize < enterprise


def test_scaled_runs_are_deterministic() -> None:
    def run(scale: OperationScale) -> list[tuple[str, str, float]]:
        engine = LegitFlowEngine()
        engine.reset(204, scenario=ScenarioId.FRESH_CUT_PROCESSOR, scale=scale)
        return [
            (event.cte_type.value, event.traceability_lot_code, event.quantity)
            for event, _ in (engine.next_event() for _ in range(50))
        ]

    assert run(OperationScale.ENTERPRISE) == run(OperationScale.ENTERPRISE)
    assert run(OperationScale.SMALL) == run(OperationScale.SMALL)


def test_api_accepts_scale_and_reports_it() -> None:
    response = client.post(
        "/api/simulate/reset",
        json={"scale": "enterprise", "delivery": {"mode": "mock"}},
    )
    assert response.status_code == 200
    status = client.get("/api/simulate/status").json()
    assert status["config"]["scale"] == "enterprise"
    assert status["stats"]["engine"]["scale"] == "enterprise"
    assert status["stats"]["engine"]["locations"] > 20

    step = client.post("/api/simulate/step").json()
    assert step["delivery_status"] == "posted"
    assert step["rejected"] == 0

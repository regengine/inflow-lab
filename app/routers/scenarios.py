from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import tenancy
from ..auth import TenantContext
from ..controller import SimulationController
from ..demo_fixtures import list_demo_fixture_summaries
from ..dependencies import get_active_controller, get_tenant_context
from ..models import (
    DemoFixtureId,
    DemoFixtureListResponse,
    DemoFixtureLoadRequest,
    DemoFixtureLoadResponse,
    DemoFixtureSummary,
    ScenarioLoadResponse,
    ScenarioListResponse,
    ScenarioSaveListResponse,
    ScenarioSaveRequest,
    ScenarioSaveResponse,
    ScenarioSaveSummary,
    ScenarioSummary,
)
from ..scenarios import ScenarioId, list_scenario_summaries


router = APIRouter(prefix="/api", tags=["Scenarios"])


@router.get("/scenarios", response_model=ScenarioListResponse)
async def list_scenarios() -> ScenarioListResponse:
    return ScenarioListResponse(
        scenarios=[ScenarioSummary.model_validate(summary) for summary in list_scenario_summaries()]
    )


@router.get("/scenario-saves", response_model=ScenarioSaveListResponse)
async def list_saved_scenarios(
    active_controller: SimulationController = Depends(get_active_controller),
) -> ScenarioSaveListResponse:
    return ScenarioSaveListResponse(
        saves=[
            ScenarioSaveSummary.model_validate(summary)
            for summary in active_controller.list_scenario_saves().saves
        ]
    )


@router.post("/scenario-saves/{scenario_id}", response_model=ScenarioSaveResponse)
async def save_scenario(
    scenario_id: ScenarioId,
    save_request: ScenarioSaveRequest | None = None,
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> ScenarioSaveResponse:
    scoped_request = tenancy.scope_scenario_save_request(context, save_request)
    return await active_controller.save_scenario(scenario_id, scoped_request)


@router.post("/scenario-saves/{scenario_id}/load", response_model=ScenarioLoadResponse)
async def load_saved_scenario(
    scenario_id: ScenarioId,
    active_controller: SimulationController = Depends(get_active_controller),
) -> ScenarioLoadResponse:
    try:
        return await active_controller.load_scenario_save(scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No saved scenario found") from exc


@router.get("/demo-fixtures", response_model=DemoFixtureListResponse)
async def list_demo_fixtures() -> DemoFixtureListResponse:
    return DemoFixtureListResponse(
        fixtures=[
            DemoFixtureSummary.model_validate(summary)
            for summary in list_demo_fixture_summaries()
        ]
    )


@router.post("/demo-fixtures/{fixture_id}/load", response_model=DemoFixtureLoadResponse)
async def load_demo_fixture(
    fixture_id: DemoFixtureId,
    fixture_request: DemoFixtureLoadRequest | None = None,
    active_controller: SimulationController = Depends(get_active_controller),
) -> DemoFixtureLoadResponse:
    return await active_controller.load_demo_fixture(fixture_id, fixture_request)

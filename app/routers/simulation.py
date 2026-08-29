from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from .. import tenancy
from ..auth import TenantContext
from ..controller import (
    SimulationController,
    config_with_stored_delivery_secrets,
    request_with_stored_delivery_secrets,
)
from ..dependencies import get_active_controller, get_tenant_context
from ..schemas.ingestion import ReplayRequest, ReplayResponse
from ..schemas.simulation import ResetResponse, SimulationConfig, StartRequest, StatusResponse, StepResponse


router = APIRouter(prefix="/api/simulate", tags=["Simulation"])


@router.get("/status", response_model=StatusResponse)
async def simulate_status(
    active_controller: SimulationController = Depends(get_active_controller),
) -> StatusResponse:
    status = await active_controller.status()
    return StatusResponse.model_validate(status)


@router.get("/stream")
async def simulate_stream(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    once: bool = Query(default=False),
    active_controller: SimulationController = Depends(get_active_controller),
) -> StreamingResponse:
    async def event_generator():
        snapshot = await active_controller.snapshot(event_limit=limit)
        last_revision = snapshot["revision"]
        yield sse_message("snapshot", snapshot)
        if once:
            return

        while True:
            if await request.is_disconnected():
                break
            try:
                last_revision = await active_controller.wait_for_revision(last_revision)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            snapshot = await active_controller.snapshot(event_limit=limit)
            last_revision = snapshot["revision"]
            yield sse_message("snapshot", snapshot)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/start", response_model=StatusResponse)
async def simulate_start(
    start_request: StartRequest,
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> StatusResponse:
    # #148: the console's form cannot round-trip the API key (status() masks
    # it), so an omitted credential here means "unchanged", not "clear it".
    config = config_with_stored_delivery_secrets(active_controller, start_request.config)
    await active_controller.start(tenancy.scope_config(context, config))
    return StatusResponse.model_validate(await active_controller.status())


@router.post("/stop", response_model=StatusResponse)
async def simulate_stop(
    active_controller: SimulationController = Depends(get_active_controller),
) -> StatusResponse:
    await active_controller.stop()
    return StatusResponse.model_validate(await active_controller.status())


@router.post("/reset", response_model=ResetResponse)
async def simulate_reset(
    config: SimulationConfig | None = None,
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> ResetResponse:
    scoped = (
        tenancy.scope_config(context, config_with_stored_delivery_secrets(active_controller, config))
        if config
        else None
    )
    await active_controller.reset(scoped)
    return ResetResponse(status="reset")


@router.post("/step", response_model=StepResponse)
async def simulate_step(
    batch_size: int | None = Query(default=None, ge=1, le=100),
    active_controller: SimulationController = Depends(get_active_controller),
) -> StepResponse:
    return await active_controller.step(batch_size=batch_size)


@router.post("/replay", response_model=ReplayResponse)
async def simulate_replay(
    replay_request: ReplayRequest | None = None,
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> ReplayResponse:
    merged = request_with_stored_delivery_secrets(active_controller, replay_request)
    return await active_controller.replay(tenancy.scope_replay_request(context, merged))


def sse_message(event_name: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n"

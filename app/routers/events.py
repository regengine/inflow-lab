from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..models import EventListResponse, LineageResponse


router = APIRouter(prefix="/api", tags=["Events"])


@router.get("/events", response_model=EventListResponse)
async def list_events(
    limit: int = Query(default=100, ge=1, le=500),
    active_controller: SimulationController = Depends(get_active_controller),
) -> EventListResponse:
    return EventListResponse(events=active_controller.store.recent(limit=limit))


@router.get("/lineage/{traceability_lot_code}", response_model=LineageResponse)
async def get_lineage(
    traceability_lot_code: str,
    active_controller: SimulationController = Depends(get_active_controller),
) -> LineageResponse:
    records = active_controller.store.lineage(traceability_lot_code)
    if not records:
        raise HTTPException(status_code=404, detail="No records found for that lot code")
    return LineageResponse(
        traceability_lot_code=traceability_lot_code,
        records=records,
        nodes=active_controller.store.lineage_nodes(records),
        edges=active_controller.store.lineage_edges(records),
    )

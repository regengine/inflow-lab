from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..schemas.exports import EventListResponse, LineageResponse


router = APIRouter(prefix="/api", tags=["Events"])

# Lineage walks a whole connected lot graph, so a single wide trace can
# match far more records than the paged /api/events feed ever returns.
# The response is bounded like every other list endpoint here, and the
# bound is reported back (`total_records` / `truncated`) so a caller can
# tell a complete trace from a clipped one instead of guessing.
LINEAGE_DEFAULT_LIMIT = 500
LINEAGE_MAX_LIMIT = 5000


@router.get("/events", response_model=EventListResponse)
async def list_events(
    limit: int = Query(default=100, ge=1, le=500),
    active_controller: SimulationController = Depends(get_active_controller),
) -> EventListResponse:
    return EventListResponse(events=active_controller.store.recent(limit=limit))


@router.get("/lineage/{traceability_lot_code}", response_model=LineageResponse)
async def get_lineage(
    traceability_lot_code: str,
    limit: int = Query(
        default=LINEAGE_DEFAULT_LIMIT,
        ge=1,
        le=LINEAGE_MAX_LIMIT,
        description=(
            "Maximum lineage records to return, oldest event first. When the "
            "trace has more, the response is truncated and `truncated` is true."
        ),
    ),
    active_controller: SimulationController = Depends(get_active_controller),
) -> LineageResponse:
    matched = active_controller.store.lineage(traceability_lot_code)
    if not matched:
        raise HTTPException(status_code=404, detail="No records found for that lot code")

    total_records = len(matched)
    # `lineage()` returns oldest-event-first, so the kept slice is the head
    # of the chain — the part that explains where the lot came from — and
    # the nodes/edges are derived from exactly the records returned, never
    # from records the caller cannot see.
    records = matched[:limit]
    return LineageResponse(
        traceability_lot_code=traceability_lot_code,
        records=records,
        nodes=active_controller.store.lineage_nodes(records),
        edges=active_controller.store.lineage_edges(records),
        total_records=total_records,
        returned_records=len(records),
        limit=limit,
        truncated=total_records > len(records),
    )

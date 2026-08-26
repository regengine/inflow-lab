from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..schemas.exports import EventListResponse, LineageResponse


router = APIRouter(prefix="/api", tags=["Events"])

# store.lineage() has no size cap of its own (#145): it does a full BFS over
# every persisted record reachable from a lot code and returns the whole
# matched set, unlike store.recent() above which /events already bounds via
# Query(..., le=500). A lot with a wide trace graph -- or one that has been
# running long enough to accumulate a lot of history -- can otherwise return
# an unbounded response. Lineage graphs are shaped differently from a flat
# recent-events feed (a single connected component that can genuinely need
# more than 500 nodes to read as complete), so this gets its own, larger
# bound rather than reusing /events' constants.
LINEAGE_DEFAULT_LIMIT = 200
LINEAGE_MAX_LIMIT = 1000


@router.get("/events", response_model=EventListResponse)
async def list_events(
    limit: int = Query(default=100, ge=1, le=500),
    active_controller: SimulationController = Depends(get_active_controller),
) -> EventListResponse:
    return EventListResponse(events=active_controller.store.recent(limit=limit))


@router.get("/lineage/{traceability_lot_code}", response_model=LineageResponse)
async def get_lineage(
    traceability_lot_code: str,
    response: Response,
    limit: int = Query(
        default=LINEAGE_DEFAULT_LIMIT,
        ge=1,
        le=LINEAGE_MAX_LIMIT,
        description="Maximum lineage records to return, oldest first.",
    ),
    active_controller: SimulationController = Depends(get_active_controller),
) -> LineageResponse:
    records = active_controller.store.lineage(traceability_lot_code)
    if not records:
        raise HTTPException(status_code=404, detail="No records found for that lot code")

    total_matched = len(records)
    truncated = total_matched > limit
    if truncated:
        # Oldest-first, matching the order store.lineage() already sorts
        # in -- a plain prefix slice rather than a re-sort or a "most
        # recent" window, so the truncated response still reads as a
        # contiguous trace starting from the same origin a full response
        # would have.
        records = records[:limit]

    # LineageResponse (app/schemas/exports.py) is owned by another
    # workstream, so truncation is signaled via headers instead of a body
    # field. A compliance tool silently getting a partial lineage back as a
    # plain 200 is worse than getting nothing -- it would read as complete
    # when it is not -- so this is never silent even when nothing is
    # truncated (#145).
    response.headers["X-Lineage-Total-Matched"] = str(total_matched)
    response.headers["X-Lineage-Truncated"] = "true" if truncated else "false"

    return LineageResponse(
        traceability_lot_code=traceability_lot_code,
        records=records,
        nodes=active_controller.store.lineage_nodes(records),
        edges=active_controller.store.lineage_edges(records),
    )

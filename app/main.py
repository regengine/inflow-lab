from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .controller import SimulationController
from .engine import LegitFlowEngine
from .mock_service import MockRegEngineService
from .models import (
    CSVImportRequest,
    CSVImportResponse,
    DeliveryRetryRequest,
    DeliveryRetryResponse,
    EventListResponse,
    IngestPayload,
    LineageResponse,
    MockIngestResponse,
    ReplayRequest,
    ReplayResponse,
    ResetResponse,
    ScenarioListResponse,
    ScenarioSummary,
    SimulationConfig,
    StartRequest,
    StatusResponse,
    StepResponse,
)
from .regengine_client import LiveRegEngineClient
from .scenarios import list_scenario_summaries
from .store import EventStore


engine = LegitFlowEngine(seed=204)
store = EventStore(persist_path="data/events.jsonl")
mock_service = MockRegEngineService()
controller = SimulationController(
    engine=engine,
    store=store,
    mock_service=mock_service,
    live_client=LiveRegEngineClient(),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await controller.shutdown()


app = FastAPI(
    title="RegEngine Inflow Lab",
    description="Mock-first FSMA 204 CTE data-flow simulator for RegEngine-compatible payloads.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "utc_time": datetime.now(UTC).isoformat(),
        "status": controller.status(),
    }


@app.get("/api/simulate/status", response_model=StatusResponse)
async def simulate_status() -> StatusResponse:
    status = controller.status()
    return StatusResponse.model_validate(status)


@app.get("/api/scenarios", response_model=ScenarioListResponse)
async def list_scenarios() -> ScenarioListResponse:
    return ScenarioListResponse(
        scenarios=[ScenarioSummary.model_validate(summary) for summary in list_scenario_summaries()]
    )


def sse_message(event_name: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n"


@app.get("/api/simulate/stream")
async def simulate_stream(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    once: bool = Query(default=False),
) -> StreamingResponse:
    async def event_generator():
        snapshot = controller.snapshot(event_limit=limit)
        last_revision = snapshot["revision"]
        yield sse_message("snapshot", snapshot)
        if once:
            return

        while True:
            if await request.is_disconnected():
                break
            try:
                last_revision = await controller.wait_for_revision(last_revision)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            snapshot = controller.snapshot(event_limit=limit)
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


@app.post("/api/simulate/start", response_model=StatusResponse)
async def simulate_start(request: StartRequest) -> StatusResponse:
    await controller.start(request.config)
    return StatusResponse.model_validate(controller.status())


@app.post("/api/simulate/stop", response_model=StatusResponse)
async def simulate_stop() -> StatusResponse:
    await controller.stop()
    return StatusResponse.model_validate(controller.status())


@app.post("/api/simulate/reset", response_model=ResetResponse)
async def simulate_reset(config: SimulationConfig | None = None) -> ResetResponse:
    await controller.reset(config)
    return ResetResponse(status="reset")


@app.post("/api/simulate/step", response_model=StepResponse)
async def simulate_step(batch_size: int | None = Query(default=None, ge=1, le=100)) -> StepResponse:
    return await controller.step(batch_size=batch_size)


@app.post("/api/simulate/replay", response_model=ReplayResponse)
async def simulate_replay(request: ReplayRequest | None = None) -> ReplayResponse:
    return await controller.replay(request)


@app.post("/api/import/csv", response_model=CSVImportResponse)
async def import_csv(request: CSVImportRequest) -> CSVImportResponse:
    return await controller.import_csv(request)


@app.post("/api/delivery/retry", response_model=DeliveryRetryResponse)
async def retry_failed_delivery(request: DeliveryRetryRequest | None = None) -> DeliveryRetryResponse:
    return await controller.retry_failed_delivery(request)


@app.get("/api/events", response_model=EventListResponse)
async def list_events(limit: int = Query(default=100, ge=1, le=500)) -> EventListResponse:
    return EventListResponse(events=store.recent(limit=limit))


@app.get("/api/lineage/{traceability_lot_code}", response_model=LineageResponse)
async def get_lineage(traceability_lot_code: str) -> LineageResponse:
    records = store.lineage(traceability_lot_code)
    if not records:
        raise HTTPException(status_code=404, detail="No records found for that lot code")
    return LineageResponse(
        traceability_lot_code=traceability_lot_code,
        records=records,
        nodes=store.lineage_nodes(records),
        edges=store.lineage_edges(records),
    )


@app.post("/api/mock/regengine/ingest", response_model=MockIngestResponse)
async def mock_regengine_ingest(payload: IngestPayload) -> MockIngestResponse:
    return mock_service.ingest(payload)


@app.get("/api/mock/regengine/export/fda-request")
async def mock_fda_request_export(
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
) -> PlainTextResponse:
    columns = [
        "Traceability Lot Code",
        "Traceability Lot Code Description",
        "Product Description",
        "Quantity",
        "Unit of Measure",
        "Location Description",
        "Location Identifier (GLN)",
        "Date",
        "Time",
        "Reference Document Type",
        "Reference Document Number",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for record in store.all_between(start_date=start_date, end_date=end_date):
        event = record.event
        writer.writerow(
            {
                "Traceability Lot Code": event.traceability_lot_code,
                "Traceability Lot Code Description": event.cte_type.value,
                "Product Description": event.product_description,
                "Quantity": event.quantity,
                "Unit of Measure": event.unit_of_measure,
                "Location Description": event.location_name,
                "Location Identifier (GLN)": engine.location_gln(event.location_name),
                "Date": event.timestamp.date().isoformat(),
                "Time": event.timestamp.time().isoformat(timespec="seconds"),
                "Reference Document Type": event.kdes.get("reference_document_type", ""),
                "Reference Document Number": event.kdes.get("reference_document_number", ""),
            }
        )
    return PlainTextResponse(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fda_request_export.csv"},
    )


@app.exception_handler(ValueError)
async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})

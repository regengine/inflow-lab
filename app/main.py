from __future__ import annotations

import asyncio
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
from .demo_fixtures import list_demo_fixture_summaries
from .engine import LegitFlowEngine
from .epcis_export import epcis_filename, render_epcis_document
from .fda_export import (
    FDA_EXPORT_PRESETS,
    apply_fda_export_preset,
    export_filename,
    list_fda_export_preset_summaries,
    render_fda_request_csv,
)
from .mock_service import MockRegEngineService
from .models import (
    CSVImportRequest,
    CSVImportResponse,
    DemoFixtureId,
    DemoFixtureListResponse,
    DemoFixtureLoadRequest,
    DemoFixtureLoadResponse,
    DemoFixtureSummary,
    DeliveryRetryRequest,
    DeliveryRetryResponse,
    EventListResponse,
    FDAExportPreset,
    FDAExportPresetListResponse,
    FDAExportPresetSummary,
    IngestPayload,
    LineageResponse,
    MockIngestResponse,
    ReplayRequest,
    ReplayResponse,
    ResetResponse,
    ScenarioLoadResponse,
    ScenarioListResponse,
    ScenarioSaveListResponse,
    ScenarioSaveRequest,
    ScenarioSaveResponse,
    ScenarioSaveSummary,
    ScenarioSummary,
    SimulationConfig,
    StartRequest,
    StatusResponse,
    StepResponse,
)
from .regengine_client import LiveRegEngineClient
from .scenario_saves import ScenarioSaveStore
from .scenarios import ScenarioId, list_scenario_summaries
from .store import EventStore


engine = LegitFlowEngine(seed=204)
store = EventStore(persist_path="data/events.jsonl")
scenario_saves = ScenarioSaveStore()
mock_service = MockRegEngineService()
controller = SimulationController(
    engine=engine,
    store=store,
    scenario_saves=scenario_saves,
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


@app.get("/api/scenario-saves", response_model=ScenarioSaveListResponse)
async def list_saved_scenarios() -> ScenarioSaveListResponse:
    return ScenarioSaveListResponse(
        saves=[
            ScenarioSaveSummary.model_validate(summary)
            for summary in controller.list_scenario_saves().saves
        ]
    )


@app.post("/api/scenario-saves/{scenario_id}", response_model=ScenarioSaveResponse)
async def save_scenario(
    scenario_id: ScenarioId,
    request: ScenarioSaveRequest | None = None,
) -> ScenarioSaveResponse:
    return await controller.save_scenario(scenario_id, request)


@app.post("/api/scenario-saves/{scenario_id}/load", response_model=ScenarioLoadResponse)
async def load_saved_scenario(scenario_id: ScenarioId) -> ScenarioLoadResponse:
    try:
        return await controller.load_scenario_save(scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No saved scenario found") from exc


@app.get("/api/demo-fixtures", response_model=DemoFixtureListResponse)
async def list_demo_fixtures() -> DemoFixtureListResponse:
    return DemoFixtureListResponse(
        fixtures=[
            DemoFixtureSummary.model_validate(summary)
            for summary in list_demo_fixture_summaries()
        ]
    )


@app.post("/api/demo-fixtures/{fixture_id}/load", response_model=DemoFixtureLoadResponse)
async def load_demo_fixture(
    fixture_id: DemoFixtureId,
    request: DemoFixtureLoadRequest | None = None,
) -> DemoFixtureLoadResponse:
    return await controller.load_demo_fixture(fixture_id, request)


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


@app.get("/api/mock/regengine/export/presets", response_model=FDAExportPresetListResponse)
async def mock_fda_request_export_presets() -> FDAExportPresetListResponse:
    return FDAExportPresetListResponse(
        presets=[
            FDAExportPresetSummary.model_validate(summary)
            for summary in list_fda_export_preset_summaries()
        ]
    )


@app.get("/api/mock/regengine/export/fda-request")
async def mock_fda_request_export(
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    preset: FDAExportPreset = Query(default=FDAExportPreset.ALL_RECORDS),
    traceability_lot_code: str | None = Query(default=None),
) -> PlainTextResponse:
    definition = FDA_EXPORT_PRESETS[preset]
    if definition.requires_lot_code and not traceability_lot_code:
        raise HTTPException(status_code=400, detail="traceability_lot_code is required for this export preset")

    if traceability_lot_code:
        records = store.lineage(traceability_lot_code)
        if not records:
            raise HTTPException(status_code=404, detail="No records found for that lot code")
        records = _filter_records_between(records, start_date=start_date, end_date=end_date)
    else:
        records = store.all_between(start_date=start_date, end_date=end_date)
    records = apply_fda_export_preset(records, preset)
    csv_text = render_fda_request_csv(records, location_gln=engine.location_gln)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={export_filename(preset)}"},
    )


@app.get("/api/mock/regengine/export/epcis")
async def mock_epcis_export(
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    traceability_lot_code: str | None = Query(default=None),
) -> JSONResponse:
    if traceability_lot_code:
        records = store.lineage(traceability_lot_code)
        if not records:
            raise HTTPException(status_code=404, detail="No records found for that lot code")
        records = _filter_records_between(records, start_date=start_date, end_date=end_date)
    else:
        records = store.all_between(start_date=start_date, end_date=end_date)

    document = render_epcis_document(
        records,
        source=controller.config.source,
        location_gln=engine.location_gln,
    )
    return JSONResponse(
        content=document,
        media_type="application/ld+json",
        headers={"Content-Disposition": f"attachment; filename={epcis_filename()}"},
    )


def _filter_records_between(
    records: list[Any],
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[Any]:
    filtered = []
    for record in records:
        day = record.event.timestamp.date().isoformat()
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        filtered.append(record)
    return sorted(filtered, key=lambda record: record.event.timestamp)


@app.exception_handler(ValueError)
async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})

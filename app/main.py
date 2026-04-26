from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import DEFAULT_TENANT_ID, TenantContext, tenant_context_from_request
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


TENANT_DATA_ROOT = Path("data/tenants")
DEFAULT_CORS_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000")

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
_tenant_controllers: dict[str, SimulationController] = {DEFAULT_TENANT_ID: controller}
_tenant_lock = RLock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for tenant_controller in set(_tenant_controllers.values()):
        await tenant_controller.shutdown()


def cors_origins_from_env() -> list[str]:
    raw_origins = os.getenv("REGENGINE_CORS_ORIGINS")
    if not raw_origins or not raw_origins.strip():
        return list(DEFAULT_CORS_ORIGINS)

    origins: list[str] = []
    for raw_origin in raw_origins.split(","):
        origin = _normalize_cors_origin(raw_origin)
        if origin and origin not in origins:
            origins.append(origin)
    return origins or list(DEFAULT_CORS_ORIGINS)


def _normalize_cors_origin(raw_origin: str) -> str | None:
    origin = raw_origin.strip().rstrip("/")
    if not origin:
        return None
    if origin == "*":
        raise ValueError("REGENGINE_CORS_ORIGINS cannot contain '*' while credentialed requests are enabled")

    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "REGENGINE_CORS_ORIGINS entries must be comma-separated HTTP(S) origins such as "
            "https://demo.example.com"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


app = FastAPI(
    title="RegEngine Inflow Lab",
    description="Mock-first FSMA 204 CTE data-flow simulator for RegEngine-compatible payloads.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_from_env(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_and_tenant_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    context = tenant_context_from_request(request)
    if isinstance(context, JSONResponse):
        return context
    request.state.tenant_context = context
    return await call_next(request)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    active_controller = _active_controller(request)
    context = _tenant_context(request)
    return {
        "ok": True,
        "utc_time": datetime.now(UTC).isoformat(),
        "tenant": context.tenant_id,
        "auth": {
            "enabled": context.auth_enabled,
            "username": context.username,
            "uses_default_storage": context.uses_default_storage,
        },
        "status": active_controller.status(),
    }


@app.get("/api/simulate/status", response_model=StatusResponse)
async def simulate_status(request: Request) -> StatusResponse:
    status = _active_controller(request).status()
    return StatusResponse.model_validate(status)


@app.get("/api/scenarios", response_model=ScenarioListResponse)
async def list_scenarios() -> ScenarioListResponse:
    return ScenarioListResponse(
        scenarios=[ScenarioSummary.model_validate(summary) for summary in list_scenario_summaries()]
    )


@app.get("/api/scenario-saves", response_model=ScenarioSaveListResponse)
async def list_saved_scenarios(request: Request) -> ScenarioSaveListResponse:
    active_controller = _active_controller(request)
    return ScenarioSaveListResponse(
        saves=[
            ScenarioSaveSummary.model_validate(summary)
            for summary in active_controller.list_scenario_saves().saves
        ]
    )


@app.post("/api/scenario-saves/{scenario_id}", response_model=ScenarioSaveResponse)
async def save_scenario(
    http_request: Request,
    scenario_id: ScenarioId,
    request: ScenarioSaveRequest | None = None,
) -> ScenarioSaveResponse:
    active_controller = _active_controller(http_request)
    scoped_request = _scope_scenario_save_request(http_request, request)
    return await active_controller.save_scenario(scenario_id, scoped_request)


@app.post("/api/scenario-saves/{scenario_id}/load", response_model=ScenarioLoadResponse)
async def load_saved_scenario(http_request: Request, scenario_id: ScenarioId) -> ScenarioLoadResponse:
    try:
        return await _active_controller(http_request).load_scenario_save(scenario_id)
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
    http_request: Request,
    fixture_id: DemoFixtureId,
    request: DemoFixtureLoadRequest | None = None,
) -> DemoFixtureLoadResponse:
    return await _active_controller(http_request).load_demo_fixture(fixture_id, request)


def sse_message(event_name: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n"


@app.get("/api/simulate/stream")
async def simulate_stream(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    once: bool = Query(default=False),
) -> StreamingResponse:
    active_controller = _active_controller(request)

    async def event_generator():
        snapshot = active_controller.snapshot(event_limit=limit)
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

            snapshot = active_controller.snapshot(event_limit=limit)
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
async def simulate_start(http_request: Request, request: StartRequest) -> StatusResponse:
    active_controller = _active_controller(http_request)
    await active_controller.start(_scope_config(http_request, request.config))
    return StatusResponse.model_validate(active_controller.status())


@app.post("/api/simulate/stop", response_model=StatusResponse)
async def simulate_stop(request: Request) -> StatusResponse:
    active_controller = _active_controller(request)
    await active_controller.stop()
    return StatusResponse.model_validate(active_controller.status())


@app.post("/api/simulate/reset", response_model=ResetResponse)
async def simulate_reset(request: Request, config: SimulationConfig | None = None) -> ResetResponse:
    await _active_controller(request).reset(_scope_config(request, config) if config else None)
    return ResetResponse(status="reset")


@app.post("/api/simulate/step", response_model=StepResponse)
async def simulate_step(
    request: Request,
    batch_size: int | None = Query(default=None, ge=1, le=100),
) -> StepResponse:
    return await _active_controller(request).step(batch_size=batch_size)


@app.post("/api/simulate/replay", response_model=ReplayResponse)
async def simulate_replay(http_request: Request, request: ReplayRequest | None = None) -> ReplayResponse:
    return await _active_controller(http_request).replay(_scope_replay_request(http_request, request))


@app.post("/api/import/csv", response_model=CSVImportResponse)
async def import_csv(http_request: Request, request: CSVImportRequest) -> CSVImportResponse:
    return await _active_controller(http_request).import_csv(request)


@app.post("/api/delivery/retry", response_model=DeliveryRetryResponse)
async def retry_failed_delivery(
    http_request: Request,
    request: DeliveryRetryRequest | None = None,
) -> DeliveryRetryResponse:
    return await _active_controller(http_request).retry_failed_delivery(request)


@app.get("/api/events", response_model=EventListResponse)
async def list_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> EventListResponse:
    return EventListResponse(events=_active_controller(request).store.recent(limit=limit))


@app.get("/api/lineage/{traceability_lot_code}", response_model=LineageResponse)
async def get_lineage(request: Request, traceability_lot_code: str) -> LineageResponse:
    active_controller = _active_controller(request)
    records = active_controller.store.lineage(traceability_lot_code)
    if not records:
        raise HTTPException(status_code=404, detail="No records found for that lot code")
    return LineageResponse(
        traceability_lot_code=traceability_lot_code,
        records=records,
        nodes=active_controller.store.lineage_nodes(records),
        edges=active_controller.store.lineage_edges(records),
    )


@app.post("/api/mock/regengine/ingest", response_model=MockIngestResponse)
async def mock_regengine_ingest(request: Request, payload: IngestPayload) -> MockIngestResponse:
    return _active_controller(request).mock_service.ingest(payload)


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
    request: Request,
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    preset: FDAExportPreset = Query(default=FDAExportPreset.ALL_RECORDS),
    traceability_lot_code: str | None = Query(default=None),
) -> PlainTextResponse:
    active_controller = _active_controller(request)
    definition = FDA_EXPORT_PRESETS[preset]
    if definition.requires_lot_code and not traceability_lot_code:
        raise HTTPException(status_code=400, detail="traceability_lot_code is required for this export preset")

    if traceability_lot_code:
        records = active_controller.store.lineage(traceability_lot_code)
        if not records:
            raise HTTPException(status_code=404, detail="No records found for that lot code")
        records = _filter_records_between(records, start_date=start_date, end_date=end_date)
    else:
        records = active_controller.store.all_between(start_date=start_date, end_date=end_date)
    records = apply_fda_export_preset(records, preset)
    csv_text = render_fda_request_csv(records, location_gln=active_controller.engine.location_gln)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={export_filename(preset)}"},
    )


@app.get("/api/mock/regengine/export/epcis")
async def mock_epcis_export(
    request: Request,
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    traceability_lot_code: str | None = Query(default=None),
) -> JSONResponse:
    active_controller = _active_controller(request)
    if traceability_lot_code:
        records = active_controller.store.lineage(traceability_lot_code)
        if not records:
            raise HTTPException(status_code=404, detail="No records found for that lot code")
        records = _filter_records_between(records, start_date=start_date, end_date=end_date)
    else:
        records = active_controller.store.all_between(start_date=start_date, end_date=end_date)

    document = render_epcis_document(
        records,
        source=active_controller.config.source,
        location_gln=active_controller.engine.location_gln,
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


def _tenant_context(request: Request) -> TenantContext:
    return getattr(request.state, "tenant_context", TenantContext(tenant_id=DEFAULT_TENANT_ID))


def _active_controller(request: Request) -> SimulationController:
    context = _tenant_context(request)
    if context.uses_default_storage:
        return controller

    with _tenant_lock:
        existing_controller = _tenant_controllers.get(context.tenant_id)
        if existing_controller is not None:
            return existing_controller

        tenant_controller = _create_tenant_controller(context.tenant_id)
        _tenant_controllers[context.tenant_id] = tenant_controller
        return tenant_controller


def _create_tenant_controller(tenant_id: str) -> SimulationController:
    persist_path = _tenant_events_path(tenant_id)
    tenant_engine = LegitFlowEngine(seed=204)
    tenant_store = EventStore(persist_path=str(persist_path))
    tenant_saves = ScenarioSaveStore(save_dir=str(_tenant_saves_path(tenant_id)))
    return SimulationController(
        engine=tenant_engine,
        store=tenant_store,
        scenario_saves=tenant_saves,
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
    )


def _scope_config(request: Request, config: SimulationConfig) -> SimulationConfig:
    context = _tenant_context(request)
    if context.uses_default_storage:
        return config
    return config.model_copy(
        update={"persist_path": str(_tenant_events_path(context.tenant_id))},
        deep=True,
    )


def _scope_replay_request(request: Request, replay_request: ReplayRequest | None) -> ReplayRequest | None:
    context = _tenant_context(request)
    if context.uses_default_storage:
        return replay_request
    request_body = replay_request or ReplayRequest()
    return request_body.model_copy(
        update={"persist_path": str(_tenant_events_path(context.tenant_id))},
        deep=True,
    )


def _scope_scenario_save_request(
    request: Request,
    save_request: ScenarioSaveRequest | None,
) -> ScenarioSaveRequest | None:
    if save_request is None or save_request.config is None:
        return save_request
    return save_request.model_copy(
        update={"config": _scope_config(request, save_request.config)},
        deep=True,
    )


def _tenant_dir(tenant_id: str) -> Path:
    return TENANT_DATA_ROOT / tenant_id


def _tenant_events_path(tenant_id: str) -> Path:
    return _tenant_dir(tenant_id) / "events.jsonl"


def _tenant_saves_path(tenant_id: str) -> Path:
    return _tenant_dir(tenant_id) / "scenario_saves"


@app.exception_handler(ValueError)
async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})

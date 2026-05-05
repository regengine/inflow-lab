from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import tenancy
from .auth_middleware import auth_and_tenant_middleware
from .build_info import APP_VERSION
from .cors import cors_origins_from_env
from .exceptions import handle_value_error
from .routers import events, health, ingestion, mock_regengine, operator, scenarios, simulation
from .tenancy import controller, scenario_saves


static_dir = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await tenancy.shutdown_tenant_controllers()


def create_app() -> FastAPI:
    fastapi_app = FastAPI(
        title="RegEngine Inflow Lab",
        description="Mock-first FSMA 204 CTE data-flow simulator for RegEngine-compatible payloads.",
        version=APP_VERSION,
        lifespan=lifespan,
    )
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins_from_env(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    fastapi_app.middleware("http")(auth_and_tenant_middleware)

    fastapi_app.mount("/static", StaticFiles(directory=static_dir), name="static")
    fastapi_app.include_router(health.router)
    fastapi_app.include_router(operator.router)
    fastapi_app.include_router(scenarios.router)
    fastapi_app.include_router(simulation.router)
    fastapi_app.include_router(ingestion.router)
    fastapi_app.include_router(events.router)
    fastapi_app.include_router(mock_regengine.router)
    fastapi_app.add_exception_handler(ValueError, handle_value_error)

    @fastapi_app.get("/")
    async def root() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return fastapi_app


app = create_app()

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import tenancy
from .auth import basic_auth_config_from_env
from .auth_middleware import auth_and_tenant_middleware
from .build_info import APP_VERSION
from .cors import cors_origins_from_env, resolve_cors_origins
from .exceptions import handle_value_error
from .routers import events, health, ingestion, integration, mock_regengine, operator, scenarios, simulation
from .tenancy import controller, scenario_saves

__all__ = ["app", "controller", "cors_origins_from_env", "create_app", "scenario_saves"]

static_dir = Path(__file__).parent / "static"

logger = logging.getLogger("inflow_lab")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not basic_auth_config_from_env().enabled:
        if _shared_deployment_requires_auth():
            # Fail closed (#88): the container binds 0.0.0.0 unconditionally,
            # so on a shared/remote deployment a forgotten auth var would
            # otherwise serve the whole state-changing API to the public
            # internet with nothing but a log line nobody reads as the
            # signal. REGENGINE_REQUIRE_AUTH opts a deployment into that
            # enforcement; see _shared_deployment_requires_auth() below.
            raise RuntimeError(
                "REGENGINE_REQUIRE_AUTH is set, so this deployment refuses to start without "
                "Basic Auth. Set REGENGINE_BASIC_AUTH_USERNAME and REGENGINE_BASIC_AUTH_PASSWORD "
                "before starting, or unset REGENGINE_REQUIRE_AUTH to run an explicit, "
                "local-loopback-only demo. See SECURITY_BOUNDARIES.md."
            )
        logger.warning(
            "inflow-lab is starting WITHOUT authentication (REGENGINE_BASIC_AUTH_USERNAME/"
            "PASSWORD unset). This is a non-production demo simulator — do not expose it on a "
            "public network or enter real/customer data. Set Basic Auth env vars, or keep it "
            "bound to localhost. See REPO_PURPOSE.md."
        )
    yield
    await tenancy.shutdown_tenant_controllers()


def _shared_deployment_requires_auth() -> bool:
    """True when REGENGINE_REQUIRE_AUTH marks this as a shared/remote profile
    that must fail closed instead of merely warning (#88).

    Off by default so a bare ``uvicorn --reload`` on a laptop keeps booting
    on just the warning above; the Dockerfile / railway.json profile is
    expected to set this explicitly for any shared or remote deployment, so
    a missing auth var fails the deploy rather than serving openly. Common
    false spellings are honored so an operator who writes "0" or "false"
    gets the opt-out they meant instead of an accidental fail-closed.
    """
    value = os.getenv("REGENGINE_REQUIRE_AUTH", "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def create_app() -> FastAPI:
    fastapi_app = FastAPI(
        title="RegEngine Inflow Lab",
        description="Mock-first FSMA 204 CTE data-flow simulator for RegEngine-compatible payloads.",
        version=APP_VERSION,
        lifespan=lifespan,
    )
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=resolve_cors_origins(),
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
    fastapi_app.include_router(integration.router)
    fastapi_app.include_router(ingestion.router)
    fastapi_app.include_router(events.router)
    fastapi_app.include_router(mock_regengine.router)
    fastapi_app.add_exception_handler(ValueError, handle_value_error)

    @fastapi_app.get("/")
    async def root() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return fastapi_app


app = create_app()

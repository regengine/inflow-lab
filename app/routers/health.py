from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..schemas.health import (
    HealthResponse,
    HealthUnavailableResponse,
    HealthzResponse,
    HealthzUnavailableResponse,
)
from ..auth import TenantContext
from ..build_info import current_build_info
from ..contract import INFLOW_CONTRACT_VERSION
from ..controller import SimulationController
from ..dependencies import get_active_controller, get_tenant_context
from ..store import EventStore


router = APIRouter(prefix="/api", tags=["Health"])

# Same convention as app/main.py: logging.getLogger("inflow_lab") is a
# global, name-keyed singleton, so this needs no import from main.py (or
# anywhere else) to land on the one app-wide logger operators already
# watch (#182).
logger = logging.getLogger("inflow_lab")

# Client-facing message only -- never the raw persist_path or OSError text.
# SECURITY_BOUNDARIES.md limits health endpoints to non-secret build/status
# metadata; the full detail (path, errno) goes to the server log instead,
# where an operator actually needs it, not to an unauthenticated response.
_STORE_UNWRITABLE_MESSAGE = "event store is not writable"


def _store_write_error(store: EventStore) -> str | None:
    """Return why ``store`` can't be written to right now, or None if it can.

    #184's own reproduction points ``persist_path`` at ``/dev/full``: a real
    device, permission bits and all, that accepts every ``open()`` and fails
    every ``write()`` with ENOSPC. A stat/``os.access()`` check reports that
    path as perfectly writable -- only an actual write ever surfaces the
    failure. So that's what this does: the same ``open("a")`` + write +
    flush sequence ``EventStore.add_many()`` uses, so it fails on exactly
    what would make a real ingest fail (full disk, detached volume,
    permission change), not a proxy for it.

    Writes a fixed-size SENTINEL FILE beside the event log, never the event
    log itself. Appending a blank line to ``persist_path`` on every poll -- as
    this first did -- is an unauthenticated, unbounded append to the tenant's
    real data file: the Docker ``HEALTHCHECK --interval=30s`` alone grows the
    persistent volume forever, and an unauthenticated caller can drive it far
    faster than that. The sentinel is opened with ``"w"``, so it is truncated
    on every probe and can never exceed one line, while still exercising the
    same mount, permission bits and free space a real ingest needs.

    Deliberately write-only: never reads ``persist_path`` back. A device
    standing in for "can't write" (like ``/dev/full``) often doesn't fail a
    *read* -- it streams zero bytes forever instead of raising or hitting
    EOF, which would turn a health check into a hang rather than a fast,
    correct "unhealthy".
    """
    if store.retired:
        # The tenant was deleted while this request was in flight (#175). The
        # probe mkdirs its parent, so probing here would recreate the tenant
        # tree the delete just removed and put it back in the operator
        # listing. Report unwritable, which is the truth.
        return "tenant has been deleted"

    probe_path, mode = _write_probe_target(store)
    try:
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        with probe_path.open(mode, encoding="utf-8") as handle:
            handle.write("ok\n")
            handle.flush()
    except OSError as exc:
        return str(exc)
    return None


def _write_probe_target(store: EventStore) -> tuple[Path, str]:
    """Where the health probe writes, and in which mode.

    Normally a fixed SENTINEL file beside the tenant's event log, opened with
    ``"w"`` so it is truncated on every probe and can never exceed one line.
    Same directory as ``persist_path`` on purpose: the probe has to fail on
    whatever would make a real ingest fail, which means the same filesystem,
    mount, free space and directory permissions. A dotfile, so it never looks
    like tenant data, and a fixed name, so it is reused rather than
    accumulated.

    The exception is a ``persist_path`` that already exists and is NOT a
    regular file -- a character device, as in #184's own reproduction against
    ``/dev/full``. A sibling sentinel there would probe the containing
    directory (``/dev``) rather than the device, and would happily succeed
    while every real ingest write failed with ENOSPC. So a device is probed
    directly, in append mode: it is the only meaningful target, and a device
    cannot grow.
    """
    persist_path = store.persist_path
    if persist_path.exists() and not persist_path.is_file():
        return persist_path, "a"
    return persist_path.parent / ".healthz-write-probe", "w"


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthUnavailableResponse}},
)
async def health(
    context: TenantContext = Depends(get_tenant_context),
    active_controller: SimulationController = Depends(get_active_controller),
) -> dict[str, Any] | JSONResponse:
    build = current_build_info().public_dict()
    write_error = _store_write_error(active_controller.store)
    if write_error is not None:
        # Full detail (path, errno) goes to the log for an operator; the
        # HTTP response below stays generic (see _STORE_UNWRITABLE_MESSAGE).
        logger.error(
            "health check failed: tenant=%s event store write failed at persist_path=%s (%s)",
            context.tenant_id,
            active_controller.store.persist_path,
            write_error,
        )
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "utc_time": datetime.now(UTC).isoformat(),
                "build": build,
                "contract_version": INFLOW_CONTRACT_VERSION,
                "tenant": context.tenant_id,
                "error": _STORE_UNWRITABLE_MESSAGE,
            },
        )
    return {
        "ok": True,
        "utc_time": datetime.now(UTC).isoformat(),
        "build": build,
        "contract_version": INFLOW_CONTRACT_VERSION,
        "tenant": context.tenant_id,
        "auth": {
            "enabled": context.auth_enabled,
            "username": context.username,
            "uses_default_storage": context.uses_default_storage,
        },
        "status": await active_controller.status(),
    }


@router.get(
    "/healthz",
    response_model=HealthzResponse,
    responses={503: {"model": HealthzUnavailableResponse}},
)
async def healthz(
    active_controller: SimulationController = Depends(get_active_controller),
) -> dict[str, Any] | JSONResponse:
    # Unauthenticated by design (see auth_and_tenant_middleware, which
    # bypasses tenant/auth resolution for exactly this path) -- it's the
    # literal target of Docker's HEALTHCHECK and railway.json's
    # healthcheckPath/restartPolicyType, both polling before or regardless
    # of whether Basic Auth is even configured. get_active_controller falls
    # back to the default tenant here for that reason, which is also
    # exactly the shared store those platform checks care about.
    write_error = _store_write_error(active_controller.store)
    if write_error is not None:
        logger.error(
            "healthz failed: event store write failed at persist_path=%s (%s)",
            active_controller.store.persist_path,
            write_error,
        )
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "utc_time": datetime.now(UTC).isoformat(),
                "build": current_build_info().public_dict(),
                "contract_version": INFLOW_CONTRACT_VERSION,
                "error": _STORE_UNWRITABLE_MESSAGE,
            },
        )
    return {
        "ok": True,
        "utc_time": datetime.now(UTC).isoformat(),
        "build": current_build_info().public_dict(),
        "contract_version": INFLOW_CONTRACT_VERSION,
    }

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import HTTPException

from .auth import DEFAULT_TENANT_ID, TENANT_HEADER, TenantContext, normalize_tenant_id
from .controller import SimulationController
from .engine import LegitFlowEngine
from .mock_service import MockRegEngineService
from .regengine_client import LiveRegEngineClient
from .scenario_saves import ScenarioSaveStore
from .schemas.ingestion import ReplayRequest
from .schemas.scenarios import ScenarioSaveRequest
from .schemas.simulation import SimulationConfig
from .store import EventStore


logger = logging.getLogger(__name__)

DATA_ROOT = Path(os.getenv("REGENGINE_DATA_DIR", "data"))
TENANT_DATA_ROOT = DATA_ROOT / "tenants"


def data_root() -> Path:
    """The simulator's storage root, resolved at call time.

    ``DATA_ROOT`` above is a snapshot taken when this module is imported, which
    is the right thing for the module-level stores it builds but the wrong
    thing for anything asked for a path *later*: a process (or a test) that
    exports ``REGENGINE_DATA_DIR`` after import would be ignored. So the
    environment wins when it is set, and the module attribute is the fallback
    -- the two agree whenever the variable was already set at import, and the
    fallback is what lets tests relocate storage by monkeypatching
    ``DATA_ROOT`` directly (see ``tests/test_release_scripts.py``).
    """
    configured = os.getenv("REGENGINE_DATA_DIR", "").strip()
    if configured:
        return Path(configured)
    return DATA_ROOT


def default_events_path() -> Path:
    """Event log for the default (non-tenant) scope.

    This is the single definition of that path. ``SimulationConfig`` defaults
    ``persist_path`` to it rather than to a literal ``data/events.jsonl``:
    the literal is CWD-relative, so on a deployment whose volume is mounted at
    ``REGENGINE_DATA_DIR=/data`` the default tenant's events landed *outside*
    the volume and were discarded on every redeploy, while tenant storage --
    derived here -- survived. See DEPLOYMENT_PROFILES.md step 4.
    """
    return data_root() / "events.jsonl"


# Selecting a tenant with ``X-RegEngine-Tenant`` lazily materializes a whole
# controller (engine, event store, scenario saves, clients) plus an on-disk
# directory. That header is honored even when Basic Auth is disabled — the
# documented default for a local demo — so without a bound an unauthenticated
# caller cycling the header value once per request could grow memory and disk
# without limit, and on a no-auth deployment the operator cleanup routes that
# could reclaim it are themselves unreachable. The cap below therefore applies
# regardless of auth state: a process serves at most this many distinct
# tenants (cached controllers plus tenant directories already on disk),
# excluding the built-in default tenant. Requests for a *new* tenant beyond
# the cap are refused with HTTP 429; existing tenants keep working.
DEFAULT_MAX_TENANTS = 100

# A tenant delete is pop-controller -> shutdown -> rmtree. Between the pop and
# the rmtree a concurrent request used to re-create and re-register a fresh
# controller, which the rmtree then left pointing at a missing directory (a
# "zombie" tenant that 500s on its next write). While a delete is in flight the
# tenant is quarantined: re-creation is refused rather than silently zombied.
# The quarantine clears as soon as the directory is gone (delete finished), and
# expires on its own so a failed rmtree cannot wedge a tenant permanently.
_DELETE_QUARANTINE_SECONDS = 30.0

engine = LegitFlowEngine(seed=204)
store = EventStore(persist_path=str(default_events_path()))
scenario_saves = ScenarioSaveStore(save_dir=str(DATA_ROOT / "scenario_saves"))
mock_service = MockRegEngineService()
controller = SimulationController(
    engine=engine,
    store=store,
    scenario_saves=scenario_saves,
    mock_service=mock_service,
    live_client=LiveRegEngineClient(),
)

_tenant_controllers: dict[str, SimulationController] = {DEFAULT_TENANT_ID: controller}
_tenants_being_deleted: dict[str, float] = {}
_tenant_lock = RLock()


async def shutdown_tenant_controllers() -> None:
    for tenant_controller in set(_tenant_controllers.values()):
        await tenant_controller.shutdown()


def active_controller_for_context(context: TenantContext) -> SimulationController:
    if context.uses_default_storage:
        return controller

    return get_tenant_controller_for_id(context.tenant_id)


def get_tenant_controller_for_id(tenant_id: str) -> SimulationController:
    with _tenant_lock:
        _reject_while_delete_in_progress(tenant_id)
        existing_controller = _tenant_controllers.get(tenant_id)
        if existing_controller is not None:
            return existing_controller

        _enforce_tenant_capacity(tenant_id)
        tenant_controller = _create_tenant_controller(tenant_id)
        _tenant_controllers[tenant_id] = tenant_controller
        return tenant_controller


def max_tenant_count() -> int:
    """Maximum number of distinct non-default tenants this process will serve."""
    raw_limit = os.getenv("REGENGINE_MAX_TENANTS", "").strip()
    if not raw_limit:
        return DEFAULT_MAX_TENANTS
    try:
        limit = int(raw_limit)
    except ValueError:
        logger.warning(
            "Ignoring malformed REGENGINE_MAX_TENANTS=%r; using default %s",
            raw_limit,
            DEFAULT_MAX_TENANTS,
        )
        return DEFAULT_MAX_TENANTS
    if limit <= 0:
        logger.warning(
            "Ignoring non-positive REGENGINE_MAX_TENANTS=%r; using default %s",
            raw_limit,
            DEFAULT_MAX_TENANTS,
        )
        return DEFAULT_MAX_TENANTS
    return limit


def _enforce_tenant_capacity(tenant_id: str) -> None:
    """Refuse to materialize a brand-new tenant once the cap is reached.

    Counts cached controllers *and* tenant directories already on disk, so the
    bound holds across restarts and covers both memory and disk growth.
    """
    limit = max_tenant_count()
    known_tenants = set(known_tenant_ids())
    if tenant_id in known_tenants or len(known_tenants) < limit:
        return
    logger.warning(
        "Refusing to create tenant %r: tenant capacity of %s reached", tenant_id, limit
    )
    raise HTTPException(
        status_code=429,
        detail=(
            f"Tenant capacity reached ({limit} tenants). Delete unused tenants or raise "
            "REGENGINE_MAX_TENANTS before creating a new one."
        ),
    )


def _reject_while_delete_in_progress(tenant_id: str) -> None:
    """Block re-creating a tenant whose delete is mid-flight (see #175)."""
    started_at = _tenants_being_deleted.get(tenant_id)
    if started_at is None:
        return
    delete_finished = not tenant_dir(tenant_id).exists()
    quarantine_expired = (time.monotonic() - started_at) > _DELETE_QUARANTINE_SECONDS
    if delete_finished or quarantine_expired:
        _tenants_being_deleted.pop(tenant_id, None)
        return
    raise HTTPException(
        status_code=409,
        detail="Tenant delete in progress; retry shortly",
    )


def pop_tenant_controller(tenant_id: str) -> SimulationController | None:
    """Remove a tenant's controller and quarantine the tenant against re-creation.

    The quarantine is what makes the caller's pop -> shutdown -> rmtree
    sequence atomic with respect to concurrent traffic for the same tenant.
    ``delete_tenant`` below clears it explicitly; callers that still run the
    sequence by hand get it cleared as soon as the directory is gone.
    """
    with _tenant_lock:
        _tenants_being_deleted[tenant_id] = time.monotonic()
        return _tenant_controllers.pop(tenant_id, None)


async def delete_tenant(tenant_id: str) -> tuple[bool, bool]:
    """Delete a tenant atomically with respect to concurrent traffic.

    Returns ``(removed_cached_controller, removed_data)``. Safe to call twice
    and safe to follow with a reset: once the directory is gone the tenant is
    no longer quarantined, so the next request creates a clean controller.
    """
    directory = assert_deletable_tenant_dir(tenant_dir(tenant_id))
    removed_data = directory.exists()
    tenant_controller = pop_tenant_controller(tenant_id)
    try:
        if tenant_controller is not None:
            await tenant_controller.shutdown()
        shutil.rmtree(directory, ignore_errors=True)
    finally:
        with _tenant_lock:
            _tenants_being_deleted.pop(tenant_id, None)
    return tenant_controller is not None, removed_data


def operator_tenant_id(raw_tenant_id: str) -> str:
    tenant_id = normalize_tenant_id(raw_tenant_id)
    if tenant_id == DEFAULT_TENANT_ID:
        raise HTTPException(status_code=400, detail="Default local tenant cannot be managed here")
    return tenant_id


def known_tenant_ids() -> list[str]:
    tenant_ids = set()
    with _tenant_lock:
        tenant_ids.update(
            tenant_id for tenant_id in _tenant_controllers if tenant_id != DEFAULT_TENANT_ID
        )

    if TENANT_DATA_ROOT.exists():
        for path in TENANT_DATA_ROOT.iterdir():
            if path.is_dir():
                try:
                    tenant_ids.add(normalize_tenant_id(path.name))
                except ValueError:
                    continue
    return sorted(tenant_ids)


def tenant_summary(tenant_id: str) -> dict[str, Any]:
    directory = tenant_dir(tenant_id)
    persist_path = tenant_events_path(tenant_id)
    with _tenant_lock:
        tenant_controller = _tenant_controllers.get(tenant_id)

    if tenant_controller is not None:
        stats = tenant_controller.store.stats()
        running = tenant_controller.running
        total_records = int(stats["total_records"])
        persist_path_text = str(tenant_controller.store.persist_path)
    else:
        running = False
        total_records = _count_jsonl_records(persist_path)
        persist_path_text = str(persist_path)

    return {
        "tenant_id": tenant_id,
        "cached": tenant_controller is not None,
        "running": running,
        "total_records": total_records,
        "scenario_save_count": _count_scenario_saves(tenant_saves_path(tenant_id)),
        "persist_path": persist_path_text,
        "data_path": str(directory),
        "exists_on_disk": directory.exists(),
    }


def _ensure_persist_path_within_root(persist_path: str) -> str:
    """Reject a caller-supplied persist_path that escapes the data root.

    In default (no-auth) local mode the caller's persist_path is used verbatim
    by the EventStore for both reads and writes. Without this guard a request
    could traverse to or target an arbitrary filesystem location
    (``../../etc/cron.d/x`` or an absolute ``/etc/passwd``), giving arbitrary
    file read/write as the service user. Tenant-scoped requests never reach
    here with a caller path — those branches override persist_path with the
    tenant's own events file.

    Raises ValueError (mapped to HTTP 400 by ``handle_value_error``) on escape.
    The message deliberately omits the offending path to avoid reflecting it.

    Staying inside the data root is necessary but not sufficient: the tenant
    directories live *under* that root, so ``data/tenants/acme/events.jsonl``
    passed the original check and pointed the shared default-tenant store
    straight at tenant ``acme``'s log -- readable through the exports and,
    because ``EventStore.reset`` unlinks its persist path, deletable by a
    plain ``POST /api/simulate/reset``. That is exactly what
    ``SECURITY_BOUNDARIES.md`` promises cannot happen ("Reset and delete
    operations must not affect other tenant scopes"), and in the default
    no-auth profile it needed no credentials at all. Tenant storage is
    therefore reachable only by sending ``X-RegEngine-Tenant``, never by
    naming a path.

    Note on relative paths: the UI submits the relative default
    ``data/events.jsonl``, while in deployments ``REGENGINE_DATA_DIR`` is often
    an *absolute* volume path. A strict within-DATA_ROOT check would then
    wrongly reject that legitimate default. A relative path with no ``..``
    escape can only target the app's own working tree (never an arbitrary
    filesystem location), so we also accept relative paths confined to the
    current working directory. Absolute paths must still be within DATA_ROOT.
    """
    candidate = Path(persist_path)
    resolved = candidate.resolve()
    root = DATA_ROOT.resolve()
    _reject_tenant_storage_path(resolved)
    if resolved == root or root in resolved.parents:
        return persist_path
    if not candidate.is_absolute():
        cwd = Path.cwd().resolve()
        if resolved == cwd or cwd in resolved.parents:
            return persist_path
    raise ValueError("persist_path must stay within the permitted data directory")


def _reject_tenant_storage_path(resolved: Path) -> None:
    """Refuse a caller-supplied path that reaches into tenant storage."""
    tenant_root = TENANT_DATA_ROOT.resolve()
    if resolved == tenant_root or tenant_root in resolved.parents:
        raise ValueError(
            "persist_path must not point into tenant storage; select a tenant with the "
            f"{TENANT_HEADER} header instead"
        )


def scope_config(context: TenantContext, config: SimulationConfig) -> SimulationConfig:
    if context.uses_default_storage:
        _ensure_persist_path_within_root(config.persist_path)
        return config
    return config.model_copy(
        update={"persist_path": str(tenant_events_path(context.tenant_id))},
        deep=True,
    )


def scope_replay_request(
    context: TenantContext,
    replay_request: ReplayRequest | None,
) -> ReplayRequest | None:
    if context.uses_default_storage:
        if replay_request is not None and replay_request.persist_path is not None:
            _ensure_persist_path_within_root(replay_request.persist_path)
        return replay_request
    request_body = replay_request or ReplayRequest()
    return request_body.model_copy(
        update={"persist_path": str(tenant_events_path(context.tenant_id))},
        deep=True,
    )


def scope_scenario_save_request(
    context: TenantContext,
    save_request: ScenarioSaveRequest | None,
) -> ScenarioSaveRequest | None:
    if save_request is None or save_request.config is None:
        return save_request
    return save_request.model_copy(
        update={"config": scope_config(context, save_request.config)},
        deep=True,
    )


def tenant_dir(tenant_id: str) -> Path:
    return TENANT_DATA_ROOT / tenant_id


def assert_deletable_tenant_dir(directory: Path) -> Path:
    """Refuse to recursively delete anything outside the tenant data root.

    A defensive assert, not a validator: every caller already runs the id
    through ``operator_tenant_id`` (strict regex, and a URL path segment
    cannot contain ``/``), so this is unreachable today. It exists because
    the consequence of the day it *is* reachable -- a ``shutil.rmtree`` on an
    attacker-influenced path -- is unrecoverable, and because the guarantee
    should hold by construction rather than by the continued correctness of a
    regex three call frames away. Resolves both sides so a symlinked tenant
    directory cannot point the delete somewhere else.
    """
    resolved = directory.resolve()
    tenant_root = TENANT_DATA_ROOT.resolve()
    if resolved == tenant_root or not resolved.is_relative_to(tenant_root):
        raise RuntimeError(
            "refusing to delete a path outside the tenant data root"
        )
    return directory


def tenant_events_path(tenant_id: str) -> Path:
    return tenant_dir(tenant_id) / "events.jsonl"


def tenant_saves_path(tenant_id: str) -> Path:
    return tenant_dir(tenant_id) / "scenario_saves"


def _count_jsonl_records(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _count_scenario_saves(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for candidate in path.glob("*.json") if candidate.is_file())


def _create_tenant_controller(tenant_id: str) -> SimulationController:
    persist_path = tenant_events_path(tenant_id)
    tenant_engine = LegitFlowEngine(seed=204)
    tenant_store = EventStore(persist_path=str(persist_path))
    tenant_saves = ScenarioSaveStore(save_dir=str(tenant_saves_path(tenant_id)))
    return SimulationController(
        engine=tenant_engine,
        store=tenant_store,
        scenario_saves=tenant_saves,
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
    )

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import HTTPException

from .auth import DEFAULT_TENANT_ID, TenantContext, normalize_tenant_id
from .controller import SimulationController
from .engine import LegitFlowEngine
from .mock_service import MockRegEngineService
from .regengine_client import LiveRegEngineClient
from .scenario_saves import ScenarioSaveStore
from .schemas.ingestion import ReplayRequest
from .schemas.scenarios import ScenarioSaveRequest
from .schemas.simulation import SimulationConfig
from .store import EventStore


# Same name-keyed singleton logger main.py configures. Tenant creation is
# the moment disk and memory get committed on a caller's say-so, so it is
# worth a line an operator can correlate against (#182).
logger = logging.getLogger("inflow_lab")


DATA_ROOT = Path(os.getenv("REGENGINE_DATA_DIR", "data"))
TENANT_DATA_ROOT = DATA_ROOT / "tenants"

# Hard ceiling on concurrently active tenant controllers (#174). Picking a
# tenant is a single request header, and honoring one lazily mints a full
# SimulationController (engine, store, scenario-save store, HTTP clients)
# plus an on-disk directory -- with no auth required to choose an id, an
# unbounded version of that lets any anonymous caller drive unbounded
# memory/disk growth for free (25 headers -> 25 controllers, verified). A
# fixed cap keeps the "try it with your own tenant id" no-auth demo
# affordance (SECURITY_BOUNDARIES.md's local-mock-demo boundary) working
# while bounding the damage; it is enforced regardless of auth state, since
# the operator cleanup routes that could otherwise reclaim capacity require
# Basic Auth and are unreachable on exactly the deployments most at risk.
# Freeing a slot needs an operator reset/delete, or a process restart.
def _parse_tenant_cap() -> int:
    raw = os.getenv("REGENGINE_MAX_TENANT_CONTROLLERS", "50")
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except (ValueError, TypeError):
        logger.warning(
            "REGENGINE_MAX_TENANT_CONTROLLERS=%r is not a valid positive integer; "
            "falling back to default (50)",
            raw,
        )
        return 50


MAX_TENANT_CONTROLLERS = _parse_tenant_cap()

engine = LegitFlowEngine(seed=204)
store = EventStore(persist_path=str(DATA_ROOT / "events.jsonl"))
scenario_saves = ScenarioSaveStore(save_dir=str(DATA_ROOT / "scenario_saves"))
# Seed the mock's hash chain from what is already persisted, so a restart
# continues the chain instead of forking a second one from an empty hash.
mock_service = MockRegEngineService(store=store)
controller = SimulationController(
    engine=engine,
    store=store,
    scenario_saves=scenario_saves,
    mock_service=mock_service,
    live_client=LiveRegEngineClient(),
    tenant_id=DEFAULT_TENANT_ID,
)

_tenant_controllers: dict[str, SimulationController] = {DEFAULT_TENANT_ID: controller}
_tenant_lock = RLock()

# Tenant ids with a delete in progress -- guards the window between
# pop_tenant_controller removing a controller from _tenant_controllers and
# the operator route finishing shutil.rmtree on its directory (#175).
# Without this, a request for the same tenant id landing in that window
# recreates the controller (and its directory) via
# get_tenant_controller_for_id, and the delete's still-pending rmtree then
# deletes that directory out from under it: the tenant stays "cached" in
# memory pointing at a path that no longer exists, and its next write
# crashes with an unhandled FileNotFoundError. Mutated only under
# _tenant_lock, alongside _tenant_controllers, so the two can never
# disagree about a tenant's state.
_tenants_being_deleted: set[str] = set()


async def shutdown_tenant_controllers() -> None:
    """Stop every tenant's run loop, concurrently and without giving up on one.

    Awaiting these serially made container shutdown pay the sum of every
    tenant's stop latency, and a platform's shutdown grace period is a fixed
    budget for all of them together (#208). `return_exceptions=True` so one
    wedged or already-broken tenant cannot stop the others from shutting down
    cleanly; each `shutdown()` is separately bounded and cancels its own run
    loop past the cap.
    """
    with _tenant_lock:
        tenant_controllers = list(set(_tenant_controllers.values()))
    if not tenant_controllers:
        return
    outcomes = await asyncio.gather(
        *(tenant_controller.shutdown() for tenant_controller in tenant_controllers),
        return_exceptions=True,
    )
    for tenant_controller, outcome in zip(tenant_controllers, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning(
                "tenant controller shutdown failed: tenant=%s (%s)",
                tenant_controller.tenant_id,
                outcome,
            )


def active_controller_for_context(context: TenantContext) -> SimulationController:
    if context.uses_default_storage:
        return controller

    return get_tenant_controller_for_id(context.tenant_id)


def get_tenant_controller_for_id(tenant_id: str) -> SimulationController:
    with _tenant_lock:
        if tenant_id in _tenants_being_deleted:
            # A delete for this tenant is mid-flight (#175): its directory
            # has just been popped from the registry and is about to be (or
            # already has been) rmtree'd by that delete. Creating a fresh
            # controller now would either get its directory pulled out from
            # under it by the pending rmtree, or resurrect a tenant the
            # caller just asked to delete. 409 tells the caller this is a
            # transient conflict to retry, instead of the unhandled
            # FileNotFoundError this used to crash with on the next write.
            raise HTTPException(
                status_code=409,
                detail=f"Tenant '{tenant_id}' delete is in progress; retry shortly",
            )

        existing_controller = _tenant_controllers.get(tenant_id)
        if existing_controller is not None:
            return existing_controller

        if _total_tenant_count() >= MAX_TENANT_CONTROLLERS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Tenant capacity reached ({MAX_TENANT_CONTROLLERS} active); "
                    "an operator must reset or delete an existing tenant before "
                    "a new one can be created"
                ),
            )

        tenant_controller = _create_tenant_controller(tenant_id)
        _tenant_controllers[tenant_id] = tenant_controller
        return tenant_controller


def pop_tenant_controller(tenant_id: str) -> SimulationController | None:
    """Remove tenant_id's controller and open a delete-in-progress window.

    Callers MUST pair this with finish_tenant_delete(tenant_id) once the
    tenant's directory has actually been removed -- typically in a
    try/finally, so the window still closes if shutdown or the rmtree
    raises. Until that call, get_tenant_controller_for_id refuses to
    re-create a controller for this tenant id instead of racing the
    in-flight delete (#175).
    """
    with _tenant_lock:
        _tenants_being_deleted.add(tenant_id)
        popped = _tenant_controllers.pop(tenant_id, None)
    if popped is not None:
        # Removing the controller from the registry only stops the NEXT
        # request resolving it. A request that resolved it before the delete
        # started still holds this object, and every EventStore write path
        # mkdirs its own parent -- so that write would recreate the tenant
        # directory after the rmtree. Retiring the store makes it fail loudly
        # instead (#175). Done outside the registry lock: retire() takes the
        # store's own lock, and holding both at once here would be the only
        # place in the codebase that orders them.
        popped.store.retire()
    return popped


def finish_tenant_delete(tenant_id: str) -> None:
    """Close the delete-in-progress window opened by pop_tenant_controller."""
    with _tenant_lock:
        _tenants_being_deleted.discard(tenant_id)


def operator_tenant_id(raw_tenant_id: str) -> str:
    tenant_id = normalize_tenant_id(raw_tenant_id)
    if tenant_id == DEFAULT_TENANT_ID:
        raise HTTPException(status_code=400, detail="Default local tenant cannot be managed here")
    return tenant_id


def _total_tenant_count() -> int:
    """Count all known tenants (in-memory + on-disk), for the cap check.

    Must be called under _tenant_lock. Enumerates the tenant directory so
    the cap holds across restarts: on-disk directories created before a
    restart still count against the limit even though they have no
    in-memory controller yet (#217).
    """
    ids: set[str] = set(_tenant_controllers.keys())
    if TENANT_DATA_ROOT.exists():
        for path in TENANT_DATA_ROOT.iterdir():
            if path.is_dir():
                try:
                    ids.add(normalize_tenant_id(path.name))
                except ValueError:
                    continue
    return len(ids)


def known_tenant_ids() -> list[str]:
    tenant_ids: set[str] = set()
    with _tenant_lock:
        tenant_ids.update(
            tenant_id for tenant_id in _tenant_controllers if tenant_id != DEFAULT_TENANT_ID
        )
        # Snapshotted under the same lock the registry is read under, so the
        # two can never disagree about a tenant's state. Subtracted at the
        # end: a directory whose pending rmtree has not reached it yet is
        # still on disk, and reporting it as a live tenant is what made a
        # deleted tenant reappear in the operator listing (#175).
        being_deleted = set(_tenants_being_deleted)

    if TENANT_DATA_ROOT.exists():
        for path in TENANT_DATA_ROOT.iterdir():
            if path.is_dir():
                try:
                    tenant_ids.add(normalize_tenant_id(path.name))
                except ValueError:
                    continue
    return sorted(tenant_ids - being_deleted)


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
    if resolved == root or root in resolved.parents:
        return persist_path
    if not candidate.is_absolute():
        cwd = Path.cwd().resolve()
        if resolved == cwd or cwd in resolved.parents:
            return persist_path
    raise ValueError("persist_path must stay within the permitted data directory")


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


def assert_within_tenant_root(path: Path) -> Path:
    """Refuse to operate on a path that is not strictly inside the tenant root.

    Resolves symlinks first, so a tenant directory symlinked elsewhere fails
    here rather than at the point something recursive runs against it. The
    root itself is refused too -- deleting one tenant must never be able to
    take out every tenant.

    The delete path is safe as written today: `operator_tenant_id` applies a
    strict regex and a URL segment cannot contain a slash. But a recursive
    delete should not rest on an upstream validator staying strict, and a
    symlink is not something that validator can see at all.
    """
    resolved_root = TENANT_DATA_ROOT.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise HTTPException(status_code=400, detail="Refusing to operate outside the tenant root")
    return resolved


def _create_tenant_controller(tenant_id: str) -> SimulationController:
    persist_path = tenant_events_path(tenant_id)
    try:
        tenant_engine = LegitFlowEngine(seed=204)
        tenant_store = EventStore(persist_path=str(persist_path))
        tenant_saves = ScenarioSaveStore(save_dir=str(tenant_saves_path(tenant_id)))
    except Exception as exc:
        # Provisioning creates directories on disk, so it can fail for reasons
        # that have nothing to do with the request that triggered it. Without
        # this the only trace was the 500 the caller got.
        logger.error("Tenant provisioning failed: tenant=%s error=%s", tenant_id, exc)
        raise
    # Logged AFTER it succeeds, and tenant id only -- never the caller's
    # credentials, which the auth layer has already discarded by this point.
    # Tenants are minted lazily on first use, so this line is the only record
    # that a new one came into existence and started consuming disk.
    logger.info("Provisioned tenant controller: tenant=%s path=%s", tenant_id, persist_path)
    return SimulationController(
        engine=tenant_engine,
        store=tenant_store,
        scenario_saves=tenant_saves,
        mock_service=MockRegEngineService(store=tenant_store),
        live_client=LiveRegEngineClient(),
        tenant_id=tenant_id,
    )

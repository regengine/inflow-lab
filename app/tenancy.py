from __future__ import annotations

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


DATA_ROOT = Path(os.getenv("REGENGINE_DATA_DIR", "data"))
TENANT_DATA_ROOT = DATA_ROOT / "tenants"

engine = LegitFlowEngine(seed=204)
store = EventStore(persist_path=str(DATA_ROOT / "events.jsonl"))
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
        existing_controller = _tenant_controllers.get(tenant_id)
        if existing_controller is not None:
            return existing_controller

        tenant_controller = _create_tenant_controller(tenant_id)
        _tenant_controllers[tenant_id] = tenant_controller
        return tenant_controller


def pop_tenant_controller(tenant_id: str) -> SimulationController | None:
    with _tenant_lock:
        return _tenant_controllers.pop(tenant_id, None)


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
    """
    candidate = Path(persist_path).resolve()
    root = DATA_ROOT.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("persist_path must stay within the permitted data directory")
    return persist_path


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

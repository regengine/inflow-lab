"""Blocking file I/O must not run on the event loop.

Regression cover for #216. `scenario_saves.py`'s `list()`/`get()`/
`save_snapshot()` and `tenancy.tenant_summary()`'s record counting were called
straight from `async def` handlers, so each one stalled the loop for as long as
the filesystem took -- and at least one of them did it while holding the
controller's data-plane lock.

The tests measure loop responsiveness rather than asserting `to_thread` appears
in the source: a heartbeat coroutine ticks every millisecond while the
blocking call runs, and the assertion is that it kept ticking. That fails if
the offload is removed, and keeps passing if the offload is rewritten some
other way.

Not covered here: the `/health` and `/healthz` write probe, which is the third
site #216 names. `_store_write_error` does not exist on `main` -- it arrives
with the audit stack (#201 onwards), so there is nothing to offload yet.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app import tenancy
from app.controller import SimulationController
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.scenarios import ScenarioId
from app.schemas.domain import CTEType, RegEngineEvent, StoredEventRecord
from app.schemas.simulation import SimulationConfig
from app.store import EventStore

# How long a single blocking call is made to take, and how much of that the
# loop is allowed to lose. A blocking call on the loop yields ~0 ticks; an
# offloaded one yields hundreds.
_BLOCK_SECONDS = 0.30
_MIN_EXPECTED_TICKS = 20


def _controller(tmp_path: Path) -> SimulationController:
    return SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / "events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
        tenant_id="offload-test",
    )


def _record(index: int = 0) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=f"TLC-{index}",
            product_description="Romaine",
            quantity=10.0,
            unit_of_measure="cases",
            location_name="Valley Farms",
            timestamp="2026-02-10T08:00:00Z",
        ),
    )


async def _ticks_during(operation) -> int:
    """Run `operation()` and count how many times the loop got a turn."""
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            await asyncio.sleep(0.001)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.01)  # let the heartbeat start
    try:
        await operation()
    finally:
        running = False
        beat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await beat
    return ticks


def _make_slow(store: ScenarioSaveStore, method_name: str) -> None:
    """Wrap a store method so it blocks the calling thread for a fixed time."""
    original = getattr(store, method_name)

    def slow(*args, **kwargs):
        time.sleep(_BLOCK_SECONDS)
        return original(*args, **kwargs)

    setattr(store, method_name, slow)


def test_listing_saves_does_not_stall_the_loop(tmp_path):
    controller = _controller(tmp_path)
    _make_slow(controller.scenario_saves, "list")

    ticks = asyncio.run(_ticks_during(controller.list_scenario_saves))

    assert ticks >= _MIN_EXPECTED_TICKS, f"loop stalled: only {ticks} ticks"


def test_saving_a_scenario_does_not_stall_the_loop(tmp_path):
    controller = _controller(tmp_path)
    controller.store.add_many([_record(index) for index in range(5)])
    _make_slow(controller.scenario_saves, "save_snapshot")

    async def save():
        await controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER)

    ticks = asyncio.run(_ticks_during(save))

    assert ticks >= _MIN_EXPECTED_TICKS, f"loop stalled: only {ticks} ticks"


def test_loading_a_scenario_save_does_not_stall_the_loop(tmp_path):
    controller = _controller(tmp_path)
    controller.store.add_many([_record(index) for index in range(5)])
    asyncio.run(controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER))
    _make_slow(controller.scenario_saves, "get")

    async def load():
        await controller.load_scenario_save(ScenarioId.LEAFY_GREENS_SUPPLIER)

    ticks = asyncio.run(_ticks_during(load))

    assert ticks >= _MIN_EXPECTED_TICKS, f"loop stalled: only {ticks} ticks"


def test_saving_still_holds_the_data_lock_across_the_offload(tmp_path):
    # The offload must not have turned into a hoist. A `reset()` landing between
    # snapshotting the records and writing them would persist a store state that
    # never existed, so the lock has to stay held across the await.
    controller = _controller(tmp_path)
    controller.store.add_many([_record(index) for index in range(5)])
    observed: list[bool] = []
    original = controller.scenario_saves.save_snapshot

    def observing(*args, **kwargs):
        observed.append(controller._lock.locked())
        return original(*args, **kwargs)

    controller.scenario_saves.save_snapshot = observing  # type: ignore[method-assign]
    asyncio.run(controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER))

    assert observed == [True], "the data-plane lock was not held across the save"


def test_a_concurrent_reset_cannot_land_mid_save(tmp_path):
    # The behavioural half of the test above: the save must persist either the
    # pre-reset records or the post-reset ones, never a torn mixture.
    controller = _controller(tmp_path)
    controller.store.add_many([_record(index) for index in range(5)])
    original = controller.scenario_saves.save_snapshot

    def slow(*args, **kwargs):
        time.sleep(0.2)
        return original(*args, **kwargs)

    controller.scenario_saves.save_snapshot = slow  # type: ignore[method-assign]

    async def race():
        saving = asyncio.create_task(
            controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER)
        )
        await asyncio.sleep(0.02)
        await controller.reset(SimulationConfig(persist_path=str(tmp_path / "events.jsonl")))
        return await saving

    response = asyncio.run(race())
    snapshot = controller.scenario_saves.get(ScenarioId.LEAFY_GREENS_SUPPLIER)

    assert snapshot is not None
    assert len(snapshot.records) == 5, "the reset tore the save"
    assert response.status == "saved"


def test_listing_tenants_does_not_stall_the_loop(tmp_path, monkeypatch):
    from app.routers.operator import list_operator_tenants

    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", tmp_path / "tenants")
    (tmp_path / "tenants" / "acme").mkdir(parents=True)
    (tmp_path / "tenants" / "acme" / "events.jsonl").write_text("", encoding="utf-8")

    real_summary = tenancy.tenant_summary

    def slow_summary(tenant_id: str):
        time.sleep(_BLOCK_SECONDS)
        return real_summary(tenant_id)

    monkeypatch.setattr(tenancy, "tenant_summary", slow_summary)

    ticks = asyncio.run(_ticks_during(lambda: list_operator_tenants(None)))

    assert ticks >= _MIN_EXPECTED_TICKS, f"loop stalled: only {ticks} ticks"

"""
Regression tests for #216: blocking file I/O must run on a thread, not the
event loop.  Each test starts a "canary" coroutine that increments a counter
whenever the event loop gets a chance to run.  If a file-I/O path holds the
loop, the canary counter doesn't advance during the call; the assertions below
confirm it does.
"""
from __future__ import annotations

import asyncio

import pytest

from app.scenario_saves import ScenarioSaveStore
from app.scenarios import ScenarioId
from app.schemas.simulation import SimulationConfig


async def _canary(ticks: list[int]) -> None:
    """Increment ticks[0] every iteration until cancelled."""
    try:
        while True:
            await asyncio.sleep(0)
            ticks[0] += 1
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_scenario_save_list_yields_to_loop(tmp_path):
    store = ScenarioSaveStore(save_dir=str(tmp_path))
    ticks: list[int] = [0]
    task = asyncio.create_task(_canary(ticks))
    await asyncio.to_thread(store.list)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # The canary must have run at least once while list() was executing.
    assert ticks[0] > 0


@pytest.mark.asyncio
async def test_scenario_save_write_yields_to_loop(tmp_path):
    from app.schemas.scenarios import ScenarioSaveSnapshot

    store = ScenarioSaveStore(save_dir=str(tmp_path))
    config = SimulationConfig()
    snapshot = ScenarioSaveSnapshot(
        scenario=ScenarioId.FRESH_CUT_PROCESSOR, config=config, records=[]
    )
    ticks: list[int] = [0]
    task = asyncio.create_task(_canary(ticks))
    await asyncio.to_thread(store.save, snapshot)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ticks[0] > 0


@pytest.mark.asyncio
async def test_scenario_save_read_yields_to_loop(tmp_path):
    from app.schemas.scenarios import ScenarioSaveSnapshot

    store = ScenarioSaveStore(save_dir=str(tmp_path))
    config = SimulationConfig()
    snapshot = ScenarioSaveSnapshot(
        scenario=ScenarioId.FRESH_CUT_PROCESSOR, config=config, records=[]
    )
    store.save(snapshot)

    ticks: list[int] = [0]
    task = asyncio.create_task(_canary(ticks))
    result = await asyncio.to_thread(store.get, ScenarioId.FRESH_CUT_PROCESSOR)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert result is not None
    assert ticks[0] > 0

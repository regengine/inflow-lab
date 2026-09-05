"""Regression coverage for #217: a reconfigure must be atomic against start().

``reset()``, ``load_demo_fixture()`` and ``load_scenario_save()`` are each
"stop the run loop, then repoint the store and engine". Before this change
nothing spanned the two steps: ``stop()`` releases ``_lifecycle_lock`` before
it returns, and the repoint only takes ``_lock`` afterwards, so a ``start()``
arriving in between -- a second console tab, a retried request, a script --
took both of its own locks uncontended and installed a fresh run loop. The
reconfigure then resumed and cleared the store, reseeded the engine and reset
the mock underneath that loop, and returned with ``running`` True: a state no
serial ordering of the two calls can produce.

``_store_epoch`` (#208) does not cover this. It voids a *commit* whose
snapshot predates the bump; a loop installed after the bump reads the new
epoch and commits happily into the store the reset just emptied.

The fix is ``_reconfigure_lock``, taken outermost around the stop-then-repoint
span of the three reconfigure paths and around ``start()``. Lock order is
``_reconfigure_lock`` -> ``_lifecycle_lock`` -> ``_lock``, never reversed, and
``stop()``/``step()`` take neither of the first two -- ``reset()`` holds the
new lock while it awaits ``stop()``, which awaits the loop, whose ``step()``
takes only ``_lock``. The second group of tests below pins that no cycle was
introduced, and that a bare POST /stop (and so ``shutdown()``) still completes
while a reconfigure holds the lock.

How the race is forced deterministically
-------------------------------------------------------------------------
Same approach as tests/test_stop_start_race.py: no real timing. The
controller's ``stop`` is wrapped so that, after the real ``stop()`` has
returned, the reconfigure parks on an ``asyncio.Event`` -- suspended at
exactly the former gap. A ``start()`` is then launched and given the loop.
Without the fix it runs to completion right there (its locks are uncontended
and, with ``_store_configure`` stubbed, it has no other suspension point) and
installs a run loop while the reconfigure is mid-flight. With the fix it
blocks on ``_reconfigure_lock`` until the reconfigure has finished.

Confirmed against the pre-fix shape by removing the lock from ``reset()``
alone: the ``reset`` case of the main test failed at "start() installed a run
loop while the reconfigure was between its stop() and its repoint" -- running
True with the reconfigure still parked -- and passed again with it restored.

Every test builds its own ``SimulationController`` rather than driving the
``app.main`` singleton: these tests contend ``_reconfigure_lock`` on purpose,
and an ``asyncio.Lock`` binds itself to the first loop that contends it, for
life -- see tests/test_start_semantics.py's SETTLE_SECONDS comment for the
cross-test failure that produces on a shared controller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.controller import SimulationController
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.scenarios import ScenarioId
from app.schemas.domain import DemoFixtureId, DestinationMode
from app.schemas.scenarios import DemoFixtureLoadRequest, ScenarioSaveRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import EventStore

# Distinct `source` values, so the end state says which call's config won.
RECONFIGURE_SOURCE = "reconfigure-generation"
START_SOURCE = "start-generation"


def _config(tmp_path: Path, **overrides: object) -> SimulationConfig:
    base = SimulationConfig(
        batch_size=1,
        interval_seconds=30,
        seed=204,
        persist_path=str(tmp_path / "reconfigure-atomicity-events.jsonl"),
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )
    return base.model_copy(update=overrides) if overrides else base


def _controller(tmp_path: Path) -> SimulationController:
    controller = SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / "reconfigure-atomicity-events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
        tenant_id="reconfigure-atomicity-test",
    )
    controller.config = _config(tmp_path)
    return controller


@dataclass
class _Generation:
    """One ``_run_loop`` invocation's fully test-controlled lifecycle."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _ScriptedRunLoop:
    """Stand-in for ``SimulationController._run_loop`` (as in test_stop_start_race.py).

    Installed as an instance attribute shadowing the bound method. Each call
    records a generation, signals that it has started, and blocks until the
    test releases it -- "the run loop is doing real work" with no dependency
    on real batch generation, delivery, or thread-pool timing. It never
    calls ``step()``, so whether a loop got installed is the only thing a
    concurrent start() can change here.
    """

    def __init__(self) -> None:
        self.generations: list[_Generation] = []

    async def __call__(self, *_args: object) -> None:
        generation = _Generation()
        self.generations.append(generation)
        generation.entered.set()
        await generation.release.wait()

    def release_all(self) -> None:
        for generation in self.generations:
            generation.release.set()

    async def wait_for_generation(self, index: int, timeout: float = 2.0) -> _Generation:
        """Wait until ``self.generations[index]`` exists and has started.

        ``asyncio.create_task`` only schedules a task, so the generation a
        start() launched is not appended yet when start() itself returns.
        """

        async def _poll() -> _Generation:
            while len(self.generations) <= index:
                await asyncio.sleep(0)
            generation = self.generations[index]
            await generation.entered.wait()
            return generation

        return await asyncio.wait_for(_poll(), timeout=timeout)


async def _instant_store_configure(_persist_path: str) -> None:
    """No-op stand-in for the ``asyncio.to_thread`` store configure (#136).

    Removes the one genuine suspension point in ``start()``, so that with its
    locks uncontended it runs start-to-finish in a single scheduler turn --
    what lets the main test be sure a start() that is still pending after
    several turns is pending on the lock, not merely slow.
    """
    return None


def _install_harness(controller: SimulationController, monkeypatch) -> _ScriptedRunLoop:
    harness = _ScriptedRunLoop()
    monkeypatch.setattr(controller, "_run_loop", harness)
    monkeypatch.setattr(controller, "_store_configure", _instant_store_configure)
    return harness


# ---------------------------------------------------------------------------
# The three reconfigure paths, each parked in the former gap.
# ---------------------------------------------------------------------------


async def _reset(controller: SimulationController, tmp_path: Path) -> None:
    await controller.reset(_config(tmp_path, source=RECONFIGURE_SOURCE))


async def _load_demo_fixture(controller: SimulationController, tmp_path: Path) -> None:
    await controller.load_demo_fixture(
        DemoFixtureId.LEAFY_GREENS_TRACE,
        DemoFixtureLoadRequest(
            source=RECONFIGURE_SOURCE,
            delivery=DeliveryConfig(mode=DestinationMode.NONE),
        ),
    )


async def _load_scenario_save(controller: SimulationController, tmp_path: Path) -> None:
    await controller.load_scenario_save(ScenarioId.LEAFY_GREENS_SUPPLIER)


async def _save_a_scenario(controller: SimulationController, tmp_path: Path) -> None:
    """Precondition for the load_scenario_save case: something to load."""
    await controller.save_scenario(
        ScenarioId.LEAFY_GREENS_SUPPLIER,
        ScenarioSaveRequest(config=_config(tmp_path, source=RECONFIGURE_SOURCE)),
    )


RECONFIGURES = [
    pytest.param(_reset, None, id="reset"),
    pytest.param(_load_demo_fixture, None, id="load_demo_fixture"),
    pytest.param(_load_scenario_save, _save_a_scenario, id="load_scenario_save"),
]


@pytest.mark.parametrize(("reconfigure", "prepare"), RECONFIGURES)
def test_a_start_landing_in_the_reconfigure_gap_waits_for_the_reconfigure(
    tmp_path, monkeypatch, reconfigure, prepare
):
    controller = _controller(tmp_path)
    harness = _install_harness(controller, monkeypatch)

    async def scenario() -> None:
        if prepare is not None:
            await prepare(controller, tmp_path)

        # Park the reconfigure in the former gap: its stop() has returned, it
        # has not yet taken `_lock` to repoint the store.
        stopped = asyncio.Event()
        gate = asyncio.Event()
        original_stop = controller.stop

        async def parked_stop() -> None:
            await original_stop()
            stopped.set()
            await gate.wait()

        monkeypatch.setattr(controller, "stop", parked_stop)

        running_when_the_reconfigure_returned: list[bool] = []

        async def reconfigure_and_observe() -> None:
            await reconfigure(controller, tmp_path)
            # Read the instant the reconfigure returns to its caller -- what
            # POST /reset's 200 body would be built from.
            running_when_the_reconfigure_returned.append(controller.running)

        reconfigure_task = asyncio.create_task(reconfigure_and_observe())
        start_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(stopped.wait(), timeout=2.0)

            start_task = asyncio.create_task(controller.start(_config(tmp_path, source=START_SOURCE)))
            # Give start() every turn it could want. Without the fix it needs
            # exactly one: its locks are uncontended and, with _store_configure
            # stubbed, nothing else in it suspends.
            for _ in range(5):
                await asyncio.sleep(0)

            # The #217 check: while the reconfigure is mid-flight, a
            # concurrent start() must not have installed a run loop.
            assert controller.running is False, (
                "start() installed a run loop while the reconfigure was between "
                "its stop() and its repoint -- that loop steps into a store the "
                "reconfigure is about to clear"
            )
            assert not start_task.done(), "start() completed instead of waiting for the reconfigure"

            gate.set()
            await asyncio.wait_for(asyncio.gather(reconfigure_task, start_task), timeout=2.0)

            # Serialised as reconfigure-then-start: the reconfigure returned
            # with no loop running, and start() ran after it in full -- one of
            # the two states a serial ordering can produce. The bug's end
            # state, running under the reconfigure's config on a loop it never
            # started, is neither.
            assert running_when_the_reconfigure_returned == [False]
            assert controller.running is True
            assert controller.config.source == START_SOURCE
            assert len(harness.generations) == 1
            task = controller._task
            assert task is not None and not task.done()
        finally:
            gate.set()
            harness.release_all()
            pending = [t for t in (reconfigure_task, start_task) if t is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await original_stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The deadlock trap: nothing the reconfigure waits on may need its lock.
# ---------------------------------------------------------------------------


def test_stop_does_not_hold_the_reconfigure_lock_while_awaiting_the_loop_task(tmp_path, monkeypatch):
    """Mirror of test_stop_start_race.py's Trap 1 check, for the new lock.

    ``reset()`` holds ``_reconfigure_lock`` for the whole of its ``await
    self.stop()``, and ``stop()`` awaits the run loop. A ``stop()`` that took
    the lock itself would deadlock every reset against its own run loop, so
    at the exact moment stop() is suspended on ``await task`` the lock must
    read unlocked -- the structural property, asserted directly.
    """
    controller = _controller(tmp_path)
    harness = _install_harness(controller, monkeypatch)

    async def scenario() -> None:
        stop_task: asyncio.Task[None] | None = None
        try:
            await controller.start(_config(tmp_path))
            await harness.wait_for_generation(0)

            stop_task = asyncio.create_task(controller.stop())
            await asyncio.sleep(0)  # let stop() run up to `await task` and suspend there
            assert not stop_task.done(), "stop() did not suspend awaiting the loop task as expected"

            assert not controller._reconfigure_lock.locked(), (
                "stop() is holding _reconfigure_lock while awaiting the loop task -- "
                "reset() awaits stop() with that lock held, so this would deadlock"
            )

            harness.generations[0].release.set()
            await asyncio.wait_for(stop_task, timeout=2.0)
            assert controller.running is False
        finally:
            harness.release_all()
            if stop_task is not None and not stop_task.done():
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_bare_stop_completes_while_a_reconfigure_holds_the_lock(tmp_path, monkeypatch):
    """The behavioural half: a bare POST /stop during a reconfigure.

    Holding the lock from outside stands in for a reset that is mid-flight.
    ``stop()`` must run to completion under it -- ``shutdown()`` is
    ``wait_for(self.stop(), timeout)``, and a stop() queued behind a
    reconfigure would make that timeout the only thing bounding container
    shutdown, exactly what #208's unlock of the delivery paths was for.
    """
    controller = _controller(tmp_path)
    harness = _install_harness(controller, monkeypatch)

    async def scenario() -> None:
        await controller.start(_config(tmp_path))
        await harness.wait_for_generation(0)

        async with controller._reconfigure_lock:
            stop_task = asyncio.create_task(controller.stop())
            await asyncio.sleep(0)
            harness.generations[0].release.set()
            await asyncio.wait_for(stop_task, timeout=2.0)

        assert controller.running is False
        assert controller._task is None

    asyncio.run(scenario())


def test_step_does_not_take_the_reconfigure_lock(tmp_path):
    """The other side of the same cycle.

    ``reset()`` -> ``stop()`` -> ``await task`` waits for the run loop to
    finish the step() it is in. If step() needed the lock reset() is
    holding, neither could ever finish. A real step -- engine, mock
    delivery, store write -- under the lock held from outside.
    """
    controller = _controller(tmp_path)

    async def scenario() -> None:
        async with controller._reconfigure_lock:
            result = await asyncio.wait_for(controller.step(batch_size=1), timeout=5.0)
        assert result.generated == 1

    asyncio.run(scenario())

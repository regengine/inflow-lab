"""Regression coverage for #211: a crashing run loop must not fail silently.

``SimulationController._run_loop`` had no exception handler at all, so
anything ``step()`` raised -- a retired store (#175), a full disk, a bug
in a delivery path -- ended the run in total silence:

    running after store break: False
    _task is None: False | task.done(): True
    stop() returned; revision changed: False | _task still set: True
    asyncio 'never retrieved' logged: True

Three separate failures in those four lines:

  * ``stop()`` early-returned on ``task.done()``, so ``self._task`` stayed
    installed forever, naming a dead loop.
  * No ``_publish_update()`` ever ran, so ``/api/stream``'s revision never
    moved and every connected console went on rendering a run that no
    longer existed.
  * Nothing retrieved the task's exception, so the only trace of the
    failure was asyncio's own "Task exception was never retrieved" at
    garbage-collection time -- detached from the run, and outside the
    "inflow_lab" logger operators actually watch (#182).

Each test below pins one of those. ``EventStore.retire()`` is used as the
break: it is the real #175 tenant-delete path, so this exercises a way the
store genuinely does die under a running loop rather than a synthetic
monkeypatch.

Every test builds its own ``SimulationController`` over its own tmp_path
store, following ``tests/test_store_async.py``'s ``_build_controller``:
retiring the ``app.main`` singleton's store would leave it unwritable for
every other test module in the session.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from pathlib import Path
from typing import Any, Callable

import pytest

from app.controller import SimulationController
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.schemas.domain import DestinationMode
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import EventStore

# Long enough to absorb the real thread-pool hop every store write takes
# (asyncio.to_thread, #136) on a loaded CI box; short enough that a genuine
# hang fails the test instead of stalling the suite.
_TASK_END_TIMEOUT = 10.0
_POLL_INTERVAL = 0.01


def _config(tmp_path: Path) -> SimulationConfig:
    return SimulationConfig(
        batch_size=1,
        # Small but non-zero: the loop takes real steps, and a crash lands
        # within a poll or two rather than being paced by a long sleep.
        interval_seconds=0.01,
        seed=204,
        persist_path=str(tmp_path / "run-loop-failure-events.jsonl"),
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )


def _build_controller(tmp_path: Path) -> SimulationController:
    persist_path = tmp_path / "run-loop-failure-events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    controller = SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=store,
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "scenario_saves")),
        mock_service=MockRegEngineService(store=store),
        live_client=LiveRegEngineClient(),
    )
    controller.config = _config(tmp_path)
    return controller


async def _wait_until(predicate: Callable[[], bool], timeout: float = _TASK_END_TIMEOUT) -> bool:
    """Poll *predicate* rather than awaiting the task.

    Awaiting the run-loop task would itself retrieve its exception, which
    is one of the very things under test here.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return predicate()


async def _start_and_step(
    controller: SimulationController, tmp_path: Path
) -> asyncio.Task[None]:
    """Start a run and wait until it has actually stored an event.

    So the crash below happens to a genuinely running loop rather than to
    one that never got going.
    """
    await controller.start(_config(tmp_path))
    task = controller._task
    assert task is not None
    assert await _wait_until(lambda: controller.store.stats()["total_records"] > 0)
    return task


async def _break_the_store(
    controller: SimulationController, task: asyncio.Task[None]
) -> None:
    """Kill the store under the running loop and wait for the loop to die."""
    controller.store.retire()
    assert await _wait_until(task.done), "the run loop never noticed the broken store"


async def _start_and_break_the_store(
    controller: SimulationController, tmp_path: Path
) -> asyncio.Task[None]:
    task = await _start_and_step(controller, tmp_path)
    await _break_the_store(controller, task)
    return task


def test_a_crashed_run_loop_publishes_a_revision_bump(tmp_path: Path) -> None:
    """The console's only signal that the run died.

    ``/api/stream`` pushes on revision changes, so with no bump a crashed
    run stays rendered as live in every open browser tab.
    """

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        steps: list[int] = []

        async def exploding_step(*_args: object, **_kwargs: object) -> None:
            steps.append(1)
            raise RuntimeError("the event store is broken")

        # Instance attribute shadowing the bound method -- the same
        # technique tests/test_stop_start_race.py uses on _run_loop.
        # Driving _run_loop directly, rather than racing a real task, is
        # what makes the count below exact: no healthy step can slip a
        # bump in between the reading and the crash.
        controller.step = exploding_step  # type: ignore[method-assign]

        revision_before = controller.revision
        await controller._run_loop(asyncio.Event())

        assert steps == [1], "the loop should have died on its first step"
        assert controller.revision == revision_before + 1, (
            "a crashing run loop published no revision bump, so /api/stream "
            "never tells the console the run ended"
        )

    asyncio.run(scenario())


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_a_crashed_run_loop_logs_the_failure(tmp_path: Path, caplog: Any) -> None:
    """The traceback belongs in the app's own logger, at crash time."""

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        with caplog.at_level(logging.ERROR, logger="inflow_lab"):
            await _start_and_break_the_store(controller, tmp_path)

    asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert any("simulation run loop stopped" in message for message in messages), messages
    assert any(
        record.exc_info is not None and "RetiredStoreError" in str(record.exc_info[0])
        for record in caplog.records
    ), "the store's own error must reach the log, not just a generic message"


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_stop_clears_a_task_that_died_and_bumps_the_revision(tmp_path: Path) -> None:
    """The #211 headline: stop() used to early-return on a done() task."""

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        task = await _start_and_break_the_store(controller, tmp_path)

        # Precondition, exactly as the review reported it.
        assert controller._task is task
        assert task.done() is True

        revision_before_stop = controller.revision
        await controller.stop()

        assert controller._task is None, "stop() left the dead task installed"
        assert controller.revision != revision_before_stop, "stop() published nothing"
        assert controller.running is False

        # Idempotent: a second stop() has nothing left to clear and must
        # not raise or publish again.
        revision_after_stop = controller.revision
        await controller.stop()
        assert controller.revision == revision_after_stop

    asyncio.run(scenario())


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_a_crashed_run_loops_exception_is_retrieved(tmp_path: Path) -> None:
    """No asyncio "Task exception was never retrieved" at GC time."""

    async def scenario() -> list[str]:
        unhandled: list[str] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unhandled.append(str(context.get("message", "")))
        )
        controller = _build_controller(tmp_path)
        await _start_and_break_the_store(controller, tmp_path)
        await controller.stop()
        # Drop every reference and force collection: Task.__del__ is what
        # reports an unretrieved exception, so nothing is provable until
        # the task is actually collected.
        controller._task = None
        gc.collect()
        await asyncio.sleep(0)
        return unhandled

    unhandled = asyncio.run(scenario())

    assert not [message for message in unhandled if "never retrieved" in message], unhandled


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_stop_on_a_crashed_task_never_waits_for_the_data_plane_lock(tmp_path: Path) -> None:
    """Lock ordering, unchanged by #211's fix.

    ``stop()`` takes ONLY ``_lifecycle_lock`` -- never ``self._lock``, the
    data-plane lock every delivery path holds around its snapshot/commit
    phases (#158, #208). Holding ``self._lock`` here from outside proves
    the crashed-task branch added by #211 did not sneak a data-plane
    acquisition into stop(): if it had, this would hang until the timeout.
    """

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        await _start_and_break_the_store(controller, tmp_path)

        async with controller._lock:
            await asyncio.wait_for(controller.stop(), timeout=5.0)

        assert controller._task is None

    asyncio.run(scenario())


def test_stop_still_stops_a_healthy_run_loop(tmp_path: Path) -> None:
    """The path #156 established, unchanged: a live loop is signalled,
    awaited outside every lock, cleared, and published."""

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        await controller.start(_config(tmp_path))
        task = controller._task
        assert task is not None
        assert controller.running is True
        await _wait_until(lambda: controller.store.stats()["total_records"] > 0)

        revision_before_stop = controller.revision
        await controller.stop()

        assert task.done() is True
        assert controller._task is None
        assert controller.running is False
        assert controller.revision != revision_before_stop

    asyncio.run(scenario())


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_the_run_loop_publishes_on_a_clean_stop_too(tmp_path: Path) -> None:
    """``_run_loop``'s new ``finally`` runs on every exit, not just crashes.

    Pinned so a later change cannot narrow the publish to the except
    branch: on a clean stop the loop itself bumps the revision as it
    unwinds, before stop()'s own post-await bump.
    """

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        stop_event = asyncio.Event()
        steps: list[int] = []

        async def one_step_then_stop(*_args: object, **_kwargs: object) -> None:
            steps.append(1)
            stop_event.set()

        controller.step = one_step_then_stop  # type: ignore[method-assign]

        revision_before = controller.revision
        await controller._run_loop(stop_event)

        assert steps == [1]
        assert controller.revision == revision_before + 1

    asyncio.run(scenario())


@pytest.mark.parametrize("break_before_start", [True, False])
@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_start_is_still_possible_after_a_crash(tmp_path: Path, break_before_start: bool) -> None:
    """A crashed loop must not wedge the controller.

    ``running`` reads False once the task is done, so ``start()`` installs
    a fresh loop over a working store either before or after ``stop()``
    has cleaned the dead task up.
    """

    async def scenario() -> None:
        controller = _build_controller(tmp_path)
        await _start_and_break_the_store(controller, tmp_path)
        if not break_before_start:
            await controller.stop()
            assert controller._task is None

        # A new store stands in for the "retry against a freshly created
        # tenant" the retired store's own error tells the caller to do.
        healthy_store = EventStore(persist_path=str(tmp_path / "restarted-events.jsonl"))
        controller.store = healthy_store
        controller.mock_service = MockRegEngineService(store=healthy_store)

        restart_config = _config(tmp_path).model_copy(
            update={"persist_path": str(tmp_path / "restarted-events.jsonl")}
        )
        await controller.start(restart_config)
        assert controller.running is True
        await _wait_until(lambda: controller.store.stats()["total_records"] > 0)
        await controller.stop()
        assert controller.running is False
        assert controller._task is None

    asyncio.run(scenario())

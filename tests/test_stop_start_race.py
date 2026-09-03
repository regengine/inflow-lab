"""Regression coverage for #156: stop() racing a concurrent start().

Filed but never implemented -- ``stop()`` was unchanged from the initial
commit until this change. The bug, confirmed directly against the
pre-fix source before writing the fix below:

    async def stop(self) -> None:
        if not self.running:
            return
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        await task
        self._task = None
        await self._publish_update()

None of this runs under ``self._lock`` -- the same lock ``start()`` uses
for every other ``self._task``/``self._stop_event`` mutation. ``await
task`` is an await point, so other coroutines run while it is suspended.
If the loop task finishes and a concurrent ``start()`` observes
``self._task.done() -> running is False`` in the window after that but
before this coroutine resumes, ``start()`` installs its own fresh task
and event -- and this method's own unconditional ``self._task = None``
then discards that reference, orphaning the new run loop. Nothing --
including a later ``stop()`` call, whose own ``if not self.running:
return`` now short-circuits on a lie -- can ever reach it again.

Two traps this fix (and this file) has to actually clear
-------------------------------------------------------------------------
Trap 1 -- deadlock: ``_run_loop`` calls ``self.step()``, which itself does
``async with self._lock``. A fix that simply wraps stop()'s *entire* body
in ``async with self._lock`` -- including the ``await task`` -- deadlocks
the moment the loop is mid-step or about to start one: step() blocks
forever on a lock stop() holds, while stop() blocks forever on a task
that can now never finish stepping. The lock below is held only for the
two brief, synchronous bracket operations (read+signal, then
compare+clear); ``await task`` itself happens with the lock released.
See ``test_stop_releases_the_lock_before_awaiting_the_loop_task`` below.

Trap 2 -- a resurrected loop: this is a risk for a *different* flawed fix
than the one below -- one that makes ``running`` go False (by clearing
``self._task``) before the old loop's task has actually, truly finished
running, e.g. by not awaiting it at all, or awaiting it without holding a
reference that prevents a concurrent start() from also thinking the coast
is clear. If that happens, a concurrent start() can install a fresh,
unset ``self._stop_event`` while the *old* loop is still actually
executing (mid-step, or about to re-check its own condition) -- and
``_run_loop``'s old shape, ``while not self._stop_event.is_set():``,
re-reads that attribute on every iteration, so the old loop would see the
new, unset event and read that as "not asked to stop," running forever
alongside the new one. The fix below cannot hit this: ``self._task`` is
only ever cleared *after* ``await task`` -- i.e. after the old task is
provably ``.done()`` -- so ``running`` can never go False while the
physical task is still executing, regardless of locking. ``_run_loop``
is still changed to take its stop_event as a parameter, captured once,
rather than reading ``self._stop_event`` at all, both because the issue
calls this out as the natural fix and because it makes the loop's
contract correct independently of however ``stop()``/``start()`` happen
to be written -- see ``test_run_loop_ignores_a_later_generations_stop_event``.

Is a brief overlap between a draining loop and a new one possible?
-------------------------------------------------------------------------
No. ``start()`` only ever creates a new task when ``not self.running``,
i.e. when ``self._task.done()`` already reads True -- and a Task is only
ever ``.done()`` once its coroutine has fully returned; there is no more
of its body left to run. So by the time any concurrent ``start()`` is
willing to install task B, task A's ``_run_loop`` has, as a hard
asyncio guarantee, already completely finished executing -- there is no
window in which A's and B's loop bodies could both be doing real work
(calling ``step()``, touching the engine/store) at once. The only
"overlap" the race below actually produces is a narrow bookkeeping one:
``self._task`` already names the new task B while this ``stop()`` call's
own continuation, having awaited the now-finished task A, has not yet
resumed to check what to do about it. That is exactly the window the
compare-before-clear below is for.

How the test forces this deterministically
-------------------------------------------------------------------------
The issue's own repro used ``asyncio.gather`` racing real start()/stop()
calls (reported as reliable -- 5000/5000) but real timing here also
crosses a real thread pool (``_store_configure`` runs EventStore.configure
via ``asyncio.to_thread``, #136), which is exactly the kind of genuine
OS-thread jitter a regression test should not depend on to pass reliably.
The main test below instead pins the interleave structurally, using two
well-defined, version-independent asyncio guarantees rather than hoping a
natural race lands the right way:

1. ``self._run_loop`` is replaced with a scripted stand-in whose
   completion is entirely test-controlled (an ``asyncio.Event`` the test
   sets), so "the old task finishes" happens exactly when the test says
   so -- never at the mercy of real batch generation, delivery, or the
   interval sleep.
2. ``self._store_configure`` is replaced with a no-op stand-in, so
   ``start()`` (with the lock uncontended, which it is here) runs
   start-to-finish with no genuine suspension point at all -- confirmed
   by the harness itself: it drives ``start()``'s coroutine with one
   manual ``.send(None)`` and asserts that raises ``StopIteration``
   (i.e. the coroutine returned) rather than yielding again.
3. That fully-synchronous ``start()`` is fired from a callback registered
   on the old task *before* ``stop()``'s own ``await task`` registers
   its continuation as a callback on the same task -- ``Task``/``Future``
   guarantee their done-callbacks run in registration order, so the
   concurrent start() is guaranteed to run, and fully complete, before
   ``stop()``'s own post-await code gets a turn. No sleep-counting, no
   "probably enough" delays.

Confirmed against the pre-fix source above: the main test below, run
before ``app/controller.py`` was changed, failed exactly as expected --
``self._task`` came back cleared (``None``) instead of still naming the
concurrently-started task, and ``controller.running`` read False while
that task was, provably, still alive and running. Re-run after the fix,
it passes, including the follow-up check that the still-referenced task
is not just present but genuinely stoppable.

Kept in its own file per this task's strict per-agent file ownership --
existing test files must not be edited. Drives ``app.main.controller``
(the shared singleton every other controller-facing test file uses)
directly rather than through ``TestClient`` -- ``test_start_semantics.py``
documents why a real background task cannot be used to set up a
"still running" precondition across two separate TestClient requests in
this harness.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.main import controller
from app.schemas.domain import DestinationMode
from app.schemas.simulation import DeliveryConfig, SimulationConfig


def setup_function() -> None:
    # Same per-test isolation as test_start_semantics.py/test_controller_
    # refactor.py: this controller is one singleton reused, unmodified,
    # across the whole session, so every test starts from a stopped,
    # default-config baseline.
    asyncio.run(controller.reset(SimulationConfig()))


def _config(tmp_path: Path, **overrides: object) -> SimulationConfig:
    base = SimulationConfig(
        batch_size=1,
        interval_seconds=30,
        seed=204,
        persist_path=str(tmp_path / "stop-start-race-events.jsonl"),
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )
    return base.model_copy(update=overrides) if overrides else base


# ---------------------------------------------------------------------------
# The main regression test: a concurrent start() must never be discarded by
# a stop() that was awaiting the task it replaced.
# ---------------------------------------------------------------------------


@dataclass
class _Generation:
    """One ``_run_loop`` invocation's fully test-controlled lifecycle."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _ScriptedRunLoop:
    """Stand-in for ``SimulationController._run_loop``.

    Installed via ``monkeypatch.setattr(controller, "_run_loop", ...)``,
    which sets an *instance* attribute shadowing the bound method, so
    ``self._run_loop(...)`` calls this instead -- tolerant of both the
    pre-fix zero-argument calling convention and the fixed
    ``self._run_loop(stop_event)`` one (``*_args``), so the very same
    harness exercises the bug against the old source and the fix against
    the new one unchanged.

    Each call is a "generation": it records itself, signals ``entered``
    (so the test can confirm the task has actually started), and then
    blocks on its own ``release`` event until the test lets it finish --
    standing in for "the run loop is doing real work" with no dependency
    on real batch generation, delivery, or thread-pool timing.
    """

    def __init__(self) -> None:
        self.generations: list[_Generation] = []

    async def __call__(self, *_args: object) -> None:
        generation = _Generation()
        self.generations.append(generation)
        generation.entered.set()
        await generation.release.wait()

    async def wait_for_generation(self, index: int, timeout: float = 2.0) -> _Generation:
        """Wait until ``self.generations[index]`` exists and has started.

        ``asyncio.create_task`` only *schedules* a task -- it does not run
        any of its body synchronously -- so a task created by ``start()``
        has not appended its ``_Generation`` yet at the moment ``start()``
        itself returns. Polling via bare ``asyncio.sleep(0)`` yields (not
        a fixed sleep) picks it up on the very next turn it actually
        exists, with no dependency on how many scheduler turns that takes.
        """

        async def _poll() -> _Generation:
            while len(self.generations) <= index:
                await asyncio.sleep(0)
            generation = self.generations[index]
            await generation.entered.wait()
            return generation

        return await asyncio.wait_for(_poll(), timeout=timeout)


async def _instant_store_configure(_persist_path: str) -> None:
    """Stand-in for ``SimulationController._store_configure``.

    The real one offloads ``EventStore.configure`` onto a worker thread
    via ``asyncio.to_thread`` (#136) -- a genuine suspension point with
    real OS-thread timing behind it. Removing that (this stand-in never
    awaits anything) is what makes ``start()`` -- given the lock is
    uncontended, which it is throughout this file -- run start-to-finish
    with no suspension point at all, which the race below depends on.
    """
    return None


def test_concurrent_start_after_the_awaited_task_finishes_does_not_orphan_it(tmp_path, monkeypatch):
    harness = _ScriptedRunLoop()
    monkeypatch.setattr(controller, "_run_loop", harness)
    monkeypatch.setattr(controller, "_store_configure", _instant_store_configure)

    config_a = _config(tmp_path, source="generation-a")
    config_b = _config(tmp_path, source="generation-b")

    async def scenario() -> None:
        task_a: asyncio.Task[None] | None = None
        task_b: asyncio.Task[None] | None = None
        # Every asyncio.Task created below gets appended here so `finally`
        # can drain all of them unconditionally, regardless of which
        # assertion (if any) fails partway through.
        spawned_tasks: list[asyncio.Task[None]] = []
        try:
            await controller.start(config_a)
            await harness.wait_for_generation(0)
            task_a = controller._task
            assert task_a is not None
            assert controller.running is True

            captured_task_b: list[asyncio.Task[None] | None] = [None]
            race_errors: list[BaseException] = []

            def _win_the_race(_finished: asyncio.Task[None]) -> None:
                # Runs as task_a's done-callback -- registered below
                # *before* stop()'s own ``await task`` registers its
                # continuation on the same task, so Task/Future's
                # documented "callbacks fire in registration order"
                # guarantees this runs, and fully completes, first.
                coro = controller.start(config_b)
                try:
                    coro.send(None)
                except StopIteration:
                    pass
                except BaseException as exc:  # pragma: no cover - defensive
                    race_errors.append(exc)
                else:
                    coro.close()
                    race_errors.append(
                        AssertionError(
                            "controller.start() suspended instead of completing "
                            "synchronously -- the harness's determinism assumption "
                            "no longer holds (a new real await point in start()?)"
                        )
                    )
                captured_task_b[0] = controller._task

            task_a.add_done_callback(_win_the_race)

            stop_task = asyncio.create_task(controller.stop())
            spawned_tasks.append(stop_task)
            await asyncio.sleep(0)  # let stop_task run up to `await task_a` and suspend there
            assert not stop_task.done(), (
                "stop() did not suspend awaiting the loop task as expected -- "
                "harness timing assumption broken"
            )

            harness.generations[0].release.set()  # let task_a finish now
            await asyncio.wait_for(stop_task, timeout=2.0)

            assert not race_errors, race_errors
            task_b = captured_task_b[0]
            assert task_b is not None, "the concurrent start() inside the callback never ran"
            assert task_b is not task_a

            # The actual #156 regression check: stop()'s cleanup must not
            # clobber a task a concurrent start() installed after the one
            # it was awaiting finished.
            assert controller._task is task_b, (
                "stop() orphaned the concurrently-started run loop: self._task "
                f"is {controller._task!r} instead of the live {task_b!r}"
            )
            assert controller.running is True
            assert not task_b.done()

            # And the reference is not just present but genuinely useful --
            # the issue's own acceptance criteria: a *later* stop() call
            # must be able to reach and stop it, matching the tracked
            # ``running`` and ``config`` for it too, not the old one's.
            assert controller.config.source == "generation-b"
            # Same signal-then-finish order as the real thing: stop() must
            # be the one to observe task_b as not-yet-done and register
            # itself as its awaiter *before* task_b actually finishes --
            # releasing generation 1 first (task_b done() before stop()
            # even starts) would hit stop()'s own "nothing to do" no-op
            # short-circuit instead (`if task is None or task.done():
            # return`, unchanged from the original `if not self.running:
            # return` and not part of #156 -- a task that finishes by some
            # means other than a stop() call was never stop()'s to clear).
            stop_task_2 = asyncio.create_task(controller.stop())
            spawned_tasks.append(stop_task_2)
            await asyncio.sleep(0)
            harness.generations[1].release.set()
            await asyncio.wait_for(stop_task_2, timeout=2.0)
            assert controller.running is False
            assert controller._task is None
            assert task_b.done()
        finally:
            for generation in harness.generations:
                generation.release.set()
            all_tasks = [t for t in (task_a, task_b, *spawned_tasks) if t is not None]
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            if all_tasks:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            # Belt-and-suspenders regardless of how the scenario above
            # went -- later tests in this file (and this shared
            # singleton's other test files) must never inherit a
            # dangling task or a stale stop_event from a race gone wrong.
            controller._task = None
            controller._stop_event = asyncio.Event()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Trap 1: stop() must not deadlock against its own run loop.
# ---------------------------------------------------------------------------


def test_stop_releases_the_lock_before_awaiting_the_loop_task(tmp_path, monkeypatch):
    """_run_loop calls self.step(), which itself does ``async with
    self._lock``. If stop() held the lock across its own ``await task``,
    step() would block forever on a lock stop() is holding, while stop()
    blocks forever on a task that can now never finish stepping -- a
    straightforward deadlock.

    This does not try to force that contention for real against a
    genuinely running loop. app.main.controller is one singleton reused,
    unmodified, for the whole pytest session (see test_start_semantics.py's
    SETTLE_SECONDS comment) -- asyncio.Lock binds itself to whichever
    event loop first *genuinely contends* it, permanently, for the life
    of the Lock object, and every test in this suite gets its own fresh
    loop via its own asyncio.run() call. test_api.py's own
    test_stop_interrupts_long_interval_sleep already calls
    controller.start() immediately followed by controller.stop() with no
    settle delay -- confirmed empirically while writing this file to
    already be enough real contention to bind self._lock to *that* test's
    loop, permanently, for every later test in the session. A second test
    that also genuinely contends the same lock in ITS OWN, different,
    loop -- which an equivalent real-contention version of this test
    reliably did -- fails outright with "<Lock ...> is bound to a
    different event loop", for a reason that has nothing to do with
    whether stop() itself deadlocks. Real deadlock guarding for the
    interval_seconds=0 rapid-loop case is exactly what a hang (rather
    than this specific RuntimeError) would look like, so it is not this
    failure mode either way.

    Using the same scripted-loop harness as the main race test above
    instead sidesteps needing any real contention at all: it asserts the
    structural property directly and deterministically -- at the exact
    moment stop() is suspended on ``await task``, self._lock must already
    read unlocked. That is precisely, and only, the condition that lets
    the loop's own step() acquire it if it needs to.
    """
    harness = _ScriptedRunLoop()
    monkeypatch.setattr(controller, "_run_loop", harness)
    monkeypatch.setattr(controller, "_store_configure", _instant_store_configure)

    async def scenario() -> None:
        task: asyncio.Task[None] | None = None
        stop_task: asyncio.Task[None] | None = None
        try:
            await controller.start(_config(tmp_path, source="deadlock-guard"))
            await harness.wait_for_generation(0)
            task = controller._task
            assert task is not None

            stop_task = asyncio.create_task(controller.stop())
            await asyncio.sleep(0)  # let stop_task run up to `await task` and suspend there
            assert not stop_task.done(), (
                "stop() did not suspend awaiting the loop task as expected -- "
                "harness timing assumption broken"
            )

            # The actual Trap 1 check: stop() must have released the lock
            # by now, not be holding it "just in case" across the await.
            assert not controller._lock.locked(), (
                "stop() is holding self._lock while awaiting the loop task -- "
                "this would deadlock the instant the loop tried to step() again"
            )

            harness.generations[0].release.set()
            await asyncio.wait_for(stop_task, timeout=2.0)
            assert controller.running is False
        finally:
            for generation in harness.generations:
                generation.release.set()
            all_tasks = [t for t in (task, stop_task) if t is not None]
            for pending_task in all_tasks:
                if not pending_task.done():
                    pending_task.cancel()
            if all_tasks:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            controller._task = None
            controller._stop_event = asyncio.Event()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Trap 2: _run_loop must honor the stop_event it was created with, not
# whatever self._stop_event happens to hold by the time it next checks.
# ---------------------------------------------------------------------------


def test_run_loop_ignores_a_later_generations_stop_event(monkeypatch):
    my_stop_event = asyncio.Event()
    step_calls = 0

    async def scripted_step(_batch_size: int | None = None) -> None:
        nonlocal step_calls
        step_calls += 1
        if step_calls == 1:
            # Simulate a start() call installing a fresh generation's
            # event on self._stop_event while this loop -- mid-iteration,
            # not yet back at its own `while` check -- has not returned.
            # A loop that read self._stop_event instead of a captured
            # parameter would see THIS fresh, never-set event on its next
            # check instead of the one it was actually told to stop with.
            controller._stop_event = asyncio.Event()
            # The actual stop signal for *this* loop -- set only on the
            # event _run_loop was called with, never on the attribute.
            my_stop_event.set()

    monkeypatch.setattr(controller, "step", scripted_step)

    async def scenario() -> None:
        await asyncio.wait_for(controller._run_loop(my_stop_event), timeout=2.0)

    asyncio.run(scenario())

    # Exactly one iteration: the loop must have stopped because it saw
    # ITS OWN stop_event set, not kept going because self._stop_event
    # (by then a different, never-set object) still read as unset.
    assert step_calls == 1

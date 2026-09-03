"""Regression coverage for #96: start() applied to an already-running simulation.

Before this fix, ``SimulationController.start()`` assigned the incoming
config's ``scenario``/``seed``/``scale``/``persist_path`` unconditionally,
but only reset the engine -- and only actually adopted a new
``persist_path`` in any way that mattered -- when the simulation was *not
already running*. Starting a running simulation with a different scenario
therefore left the old engine emitting the old scenario's events (into,
potentially, the wrong file) while ``self.config`` -- and everything
derived from it via ``status()``, including the industry audit -- reported
the new one. A single ``status()`` payload contradicted itself, and the
audit evaluated real events against the wrong scenario's rules.

Chosen fix -- reject, don't silently apply or silently drop
-----------------------------------------------------------
``start()`` now raises a plain ``fastapi.HTTPException(status_code=409)``
(the same pattern already used outside a router in
``app/tenancy.py::get_tenant_controller_for_id`` for an analogous
transient-conflict case) when called on an already-running simulation whose
incoming config differs from the current one in any of the four fields that
actually reshape what gets produced: ``seed``, ``scenario``, ``scale``, or
``persist_path``. Raising ``HTTPException`` directly from the controller --
rather than a plain ``ValueError`` -- is required here: this repo's only
app-wide exception handler (``app/exceptions.py::handle_value_error``, wired
in ``app/main.py``) maps ``ValueError`` to HTTP 400, and neither
``app/main.py`` nor ``app/routers/simulation.py`` is ownable by this change,
so an ``HTTPException`` raised anywhere in the call stack -- which
Starlette's default exception handling picks up regardless of how deep it
was raised, with zero router/app wiring changes needed -- is the only way to
surface a genuine 409 without touching either file.

Every other field (``source``, ``batch_size``, ``interval_seconds``,
``delivery``) is read fresh off ``self.config`` by ``step()``/``_run_loop()``
on every iteration, so changing *those* on a running simulation still just
works, live, with no reset and no conflict -- this is what acceptance
criterion 4 on the issue calls "remain supported".

Why reject, rather than the other two options the issue lists
---------------------------------------------------------------
* Silently keeping the old engine's scenario while now also *reporting* it
  correctly (the issue's "at minimum" option -- move the assignments inside
  ``if not self.running``) still drops the caller's request with no signal
  that anything was ignored. It relocates the exact mismatch the issue is
  about from "config vs. engine" to "config vs. what the caller actually
  asked for" -- self-consistent on the wire, still silently wrong.
* Auto-restarting (stop the old run, apply the new config, reset the
  engine, start a fresh task -- mirroring ``load_demo_fixture``'s existing
  ``await self.stop()`` ordering) makes ``start()`` silently discard
  whatever the current run was doing the moment two callers disagree about
  what should be running, with no confirmation either side asked for that.
  It would also add a new call site into ``stop()`` for every conflicting
  ``start()`` -- and ``stop()`` still does not acquire ``self._lock`` while
  it awaits the running task (issue #156, filed against this exact code
  path). That race is real but is a separate, still-open concern this
  change deliberately does not touch (no test below exercises it); adding
  a new caller into it would only grow its surface area for no benefit,
  since rejecting needs no new ``stop()`` call at all.

Read against the console before choosing
-----------------------------------------
``syncRunButtons()`` in app/static/app.js disables the Start button for the
*entire* time ``status.running`` is true, and only re-enables it once Stop
has completed and the UI has refreshed -- confirmed independently by
``scripts/browser_smoke.py``'s own comment: "Start/Pause mirror the loop
state: pausing is only possible while running, starting only while idle."
The console already treats "running" and "start" as mutually exclusive; an
operator clicking through the UI can never reach the conflict branch this
file tests. Only a direct API call, or a second browser tab whose own view
of ``running`` has gone stale, can -- and for both, a clear 409 is strictly
better than a status payload that quietly disagrees with itself. A fix that
made the console's own normal flow start erroring would be worse than the
bug; this one cannot, because the UI never sends the request that triggers
it.

Every behavioural test below asserts on real emitted events (stored
records' own ``product_description``, or persisted-file content), not only
on ``status()``'s labels -- pinning that the two can never again just
happen to agree on paper while the underlying data disagrees, which is
exactly how #96 hid for as long as it did.

Kept in its own file per this task's strict per-agent file ownership --
existing test files must not be edited.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.controller import SimulationController, _engine_shaping_fields_changed
from app.main import app, controller
from app.scenarios import ScenarioId, get_scenario
from app.schemas.domain import DestinationMode, OperationScale
from app.schemas.simulation import DeliveryConfig, SimulationConfig


client = TestClient(app)

LEAFY = ScenarioId.LEAFY_GREENS_SUPPLIER
DAIRY = ScenarioId.DAIRY_CONTINUOUS_FLOW

# Disjoint product catalogs (verified against app/scenarios.py) -- an event
# naming a dairy product can only have come from a dairy-scenario engine,
# never a leafy_greens_supplier one, and vice versa. That makes them a
# reliable, non-circular way to check what actually generated an event,
# independent of whatever status() *says* generated it.
LEAFY_PRODUCTS = {product.name for product in get_scenario(LEAFY).products}
DAIRY_PRODUCTS = {product.name for product in get_scenario(DAIRY).products}
assert not (LEAFY_PRODUCTS & DAIRY_PRODUCTS)


def setup_function() -> None:
    # Same per-test isolation every other controller-facing test file in
    # this suite uses (test_api.py, test_operation_scale.py, ...): stop and
    # reset the shared app.main.controller before each test so no test
    # leaks a running task or leftover config into the next one.
    asyncio.run(controller.reset(SimulationConfig()))


# SETTLE_SECONDS: how long _start_and_settle() (below) waits for _run_loop's
# automatic first batch before the calling test does anything else that
# touches self._lock.
#
# app.main.controller is one singleton reused, unmodified, across every test
# in this whole pytest session, each of which drives it through its OWN
# asyncio.run() call -- a fresh event loop, used once, then closed. Right
# after start() launches _run_loop as a background task, that task and this
# test's own next await are both independently scheduled on the SAME loop,
# racing to go next; if the test's own next call reaches self._lock first
# and the background task then has to actually wait for it (contention),
# asyncio.Lock resolves that by calling _get_loop() -- which BINDS the lock
# to whichever loop is running at that moment, permanently, for the
# lifetime of the Lock object (see CPython's asyncio.mixins._LoopBoundMixin:
# an uncontended acquire() never calls _get_loop() at all, so this binding
# only happens on genuine contention). Since self._lock outlives any one
# test's loop, the next test whose background task also contends for it --
# in ITS OWN, different, freshly-created loop -- fails outright with
# "<Lock ...> is bound to a different event loop", killing that later
# test's background task for a reason that has nothing to do with what it
# is actually testing (and, as a direct consequence, flipping running()
# back to False out from under it).
#
# Waiting here for the automatic first batch to finish before this test's
# own next lock-touching call keeps every acquire() in this file on the
# fast, uncontended path, so self._lock is never asked to straddle two
# loops in the first place. Generous on purpose (mock delivery plus one
# small local file append, normally sub-millisecond) rather than tuned to
# the minimum that happens to pass today.
SETTLE_SECONDS = 0.2


async def _start_and_settle(config: SimulationConfig) -> None:
    """await controller.start(config), then let _run_loop's automatic first
    batch finish -- see SETTLE_SECONDS above for why this matters here.
    """
    await controller.start(config)
    await asyncio.sleep(SETTLE_SECONDS)


def _config(tmp_path: Path, **overrides: object) -> SimulationConfig:
    base = SimulationConfig(
        scenario=LEAFY,
        seed=204,
        scale=OperationScale.MIDSIZE,
        batch_size=1,
        # Long enough that _run_loop's post-first-batch wait never resolves
        # on its own during a test -- the only automatic batch a test can
        # observe is the one _run_loop always fires immediately on start,
        # before it ever waits on this interval for the first time.
        interval_seconds=60,
        persist_path=str(tmp_path / "base-events.jsonl"),
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )
    # model_copy(), not SimulationConfig(**fields): the latter's keyword
    # types are all pinned to their own field (ScenarioId, float, ...), so
    # splatting one shared **overrides: object dict into it does not type
    # check no matter which single field a given call actually overrides.
    return base.model_copy(update=overrides) if overrides else base


# ---------------------------------------------------------------------------
# _engine_shaping_fields_changed: the pure helper directly.
# ---------------------------------------------------------------------------


def test_engine_shaping_fields_changed_only_tracks_seed_scenario_scale(tmp_path):
    base = _config(tmp_path)

    assert _engine_shaping_fields_changed(base, base.model_copy()) is False
    assert _engine_shaping_fields_changed(base, base.model_copy(update={"seed": 999})) is True
    assert _engine_shaping_fields_changed(base, base.model_copy(update={"scenario": DAIRY})) is True
    assert (
        _engine_shaping_fields_changed(base, base.model_copy(update={"scale": OperationScale.ENTERPRISE}))
        is True
    )
    # LegitFlowEngine.reset() takes none of these -- they must NOT register
    # as engine-shaping, or start() would refuse harmless live tuning too.
    assert _engine_shaping_fields_changed(base, base.model_copy(update={"persist_path": "x"})) is False
    assert _engine_shaping_fields_changed(base, base.model_copy(update={"batch_size": 9})) is False
    assert _engine_shaping_fields_changed(base, base.model_copy(update={"interval_seconds": 5})) is False
    assert _engine_shaping_fields_changed(base, base.model_copy(update={"source": "other"})) is False


# ---------------------------------------------------------------------------
# The core of #96: start() on a running simulation with a conflicting
# engine-shaping field is rejected, and the running engine keeps emitting
# the scenario it was actually reset to -- never the rejected one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scenario", DAIRY),
        ("seed", 999),
        ("scale", OperationScale.ENTERPRISE),
    ],
)
def test_start_while_running_rejects_a_conflicting_engine_shaping_field(tmp_path, field, value):
    async def scenario() -> None:
        await _start_and_settle(_config(tmp_path))
        assert controller.running is True

        with pytest.raises(HTTPException) as excinfo:
            await controller.start(_config(tmp_path, **{field: value}))
        assert excinfo.value.status_code == 409

        # Nothing moved: config, the engine snapshot it must agree with,
        # and the run itself are all exactly as they were pre-rejection --
        # the exact invariant the issue's acceptance criteria names.
        assert controller.config.scenario == LEAFY
        assert controller.config.seed == 204
        assert controller.config.scale == OperationScale.MIDSIZE
        assert controller.engine.snapshot()["scenario"] == LEAFY.value
        assert controller.running is True

        # The "events keep coming from the old one" half of the issue: ask
        # the still-running engine for one more batch. It must still be
        # leafy_greens_supplier output, never dairy_continuous_flow,
        # regardless of which single field the rejected call tried to change.
        before_ids = {record.record_id for record in controller.store.all_between()}
        await controller.step(batch_size=1)
        new_records = [
            record for record in controller.store.all_between() if record.record_id not in before_ids
        ]
        assert len(new_records) == 1
        assert new_records[0].event.product_description in LEAFY_PRODUCTS
        assert new_records[0].event.product_description not in DAIRY_PRODUCTS

        await controller.stop()

    try:
        asyncio.run(scenario())
    finally:
        # Belt-and-suspenders: if an assertion above failed mid-scenario,
        # make sure no later test in this file inherits a running task.
        asyncio.run(controller.stop())


def test_start_while_running_rejects_a_different_persist_path_and_never_splits_output(tmp_path):
    original_path = tmp_path / "original-run.jsonl"
    other_path = tmp_path / "should-never-be-created.jsonl"

    async def scenario() -> None:
        await _start_and_settle(_config(tmp_path, persist_path=str(original_path)))
        assert controller.running is True
        await controller.step(batch_size=1)

        with pytest.raises(HTTPException) as excinfo:
            await controller.start(_config(tmp_path, persist_path=str(other_path)))
        assert excinfo.value.status_code == 409

        # EventStore.configure() both repoints persist_path AND reloads the
        # in-memory ring from that file -- a rejected attempt must not have
        # touched either half.
        assert controller.store.persist_path == original_path
        assert not other_path.exists()

        await controller.step(batch_size=1)
        assert controller.store.persist_path == original_path
        assert not other_path.exists()

        await controller.stop()

        # Only safe to read the file directly once stop() has awaited the
        # run loop's own task to completion -- otherwise a background
        # add_many() on its worker thread could still be mid-write.
        on_disk_lines = original_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(on_disk_lines) == len(controller.store.all_between()) > 0

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(controller.stop())


def test_start_while_running_with_a_byte_identical_config_is_not_a_conflict(tmp_path):
    # A client that resends the exact same start() request it already
    # issued (a retried request after a network hiccup, for instance)
    # must get the same success back, not a spurious 409 -- nothing about
    # this request actually conflicts with what is already running.
    async def scenario() -> None:
        config = _config(tmp_path)
        await _start_and_settle(config)
        assert controller.running is True

        await controller.start(config.model_copy())

        assert controller.running is True
        assert controller.config.scenario == LEAFY
        assert controller.engine.snapshot()["scenario"] == LEAFY.value

        await controller.stop()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(controller.stop())


def test_start_while_running_still_applies_batch_size_interval_and_source_live(tmp_path):
    # Acceptance criterion 4 on the issue: batch_size/interval_seconds
    # updates on a running simulation must remain supported (or be
    # explicitly rejected, with a test either way). Chosen behaviour here:
    # supported, live, with no engine reset -- these fields are not part of
    # LegitFlowEngine.reset()'s signature, so nothing needs resetting for
    # them to take effect on the very next iteration.
    async def scenario() -> None:
        await _start_and_settle(_config(tmp_path, batch_size=1, source="line-a"))
        assert controller.running is True

        await controller.start(_config(tmp_path, batch_size=4, interval_seconds=45, source="line-b"))

        assert controller.running is True
        assert controller.config.batch_size == 4
        assert controller.config.interval_seconds == 45
        assert controller.config.source == "line-b"
        # Still the same engine/scenario throughout -- this was never a
        # conflict, so the engine must not have been touched either.
        assert controller.engine.snapshot()["scenario"] == LEAFY.value

        # Prove the update is live rather than trusting the label: step()
        # with no explicit batch_size now reads self.config.batch_size
        # fresh, and each stored record's own payload_source reflects the
        # new source -- both only true if start() actually applied the
        # update to the running simulation instead of silently dropping it.
        before_ids = {record.record_id for record in controller.store.all_between()}
        result = await controller.step()
        assert result.generated == 4
        new_records = [
            record for record in controller.store.all_between() if record.record_id not in before_ids
        ]
        assert len(new_records) == 4
        assert all(record.payload_source == "line-b" for record in new_records)

        await controller.stop()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(controller.stop())


def test_start_while_not_running_still_resets_the_engine_on_scenario_change(tmp_path):
    # Regression pin for the refactor in this change: the pre-existing
    # not-running branch (skip engine.reset() unless an engine-shaping
    # field actually changed) now goes through the same
    # _engine_shaping_fields_changed() helper as the new running-conflict
    # guard, and must keep behaving exactly as it did before.
    async def scenario() -> None:
        await controller.reset(_config(tmp_path, scenario=LEAFY))
        assert controller.running is False
        await controller.step(batch_size=1)  # accumulate a leafy record without starting the loop

        await _start_and_settle(_config(tmp_path, scenario=DAIRY))
        assert controller.running is True
        assert controller.config.scenario == DAIRY
        assert controller.engine.snapshot()["scenario"] == DAIRY.value

        before_ids = {record.record_id for record in controller.store.all_between()}
        await controller.step(batch_size=1)
        new_records = [
            record for record in controller.store.all_between() if record.record_id not in before_ids
        ]
        assert len(new_records) == 1
        assert new_records[0].event.product_description in DAIRY_PRODUCTS

        await controller.stop()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(controller.stop())


# ---------------------------------------------------------------------------
# End-to-end, through the real (unedited) /api/simulate/start route: proves
# the HTTPException raised inside the controller actually surfaces as a 409
# over HTTP with no router or app-wiring changes, and pins the issue's own
# acceptance-criteria wording -- config.scenario and stats.engine.scenario
# must never disagree.
#
# These two tests force `running` via monkeypatch rather than keeping a
# genuinely running background task alive across two separate TestClient
# requests, because -- confirmed empirically while writing this file --
# that does not work in this harness: Starlette's TestClient runs each
# request inside its own anyio scope, and a task started via
# asyncio.create_task() during one request (exactly what start() does) does
# not survive past that request's own scope here. A response body's own
# "running" field does read True (checked directly against
# app.main.controller immediately after a client.post("/api/simulate/start")
# call returns), but a second, separate request already sees running as
# False again -- so a real background task cannot be used to set up a
# "still running" precondition for a *later*, separate HTTP call. That is a
# property of exercising background tasks through this specific client, not
# of the real app under a real ASGI server (where the task lives on one
# persistent server loop and genuinely outlives any single request) -- so
# it says nothing about start()'s conflict-detection logic itself, which
# every test above already covers thoroughly with a genuinely running
# background task via direct asyncio.run() calls. What those tests can't
# reach is this app's specific exception-handling stack (the app-wide
# ValueError -> 400 handler, auth/CORS middleware): forcing `running` here
# isolates exactly that question -- does a plain HTTPException(409) raised
# deep in the controller reach an HTTP caller of the real, unedited route
# unmolested -- without depending on the background task's lifetime at all.
# ---------------------------------------------------------------------------


def test_start_endpoint_returns_409_and_leaves_state_untouched_when_running_conflicts(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationController, "running", property(lambda self: True))
    # setup_function()'s controller.reset(SimulationConfig()) already left
    # config.scenario at the schema default (leafy_greens_supplier).
    assert controller.config.scenario == LEAFY

    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "scenario": "dairy_continuous_flow",
                "seed": 999,
                "batch_size": 1,
                "interval_seconds": 60,
                "persist_path": str(tmp_path / "forced-conflict.jsonl"),
            }
        },
    )

    assert response.status_code == 409
    assert "stop" in response.json()["detail"].lower()

    # Rejected before any mutation: config, and everything status() derives
    # from it, is exactly what it was pre-request -- the two must not
    # disagree, which is the whole point of the issue's acceptance criteria.
    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == status["stats"]["engine"]["scenario"] == "leafy_greens_supplier"
    assert controller.config.seed == 204
    assert not (tmp_path / "forced-conflict.jsonl").exists()


def test_start_endpoint_still_applies_safe_field_changes_over_http_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationController, "running", property(lambda self: True))
    current = controller.config

    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                # Every engine/store-shaping field held exactly at its
                # current value -- this request does not conflict with
                # anything, "running" or not.
                "scenario": current.scenario.value,
                "seed": current.seed,
                "scale": current.scale.value,
                "persist_path": current.persist_path,
                "batch_size": 6,
                "interval_seconds": 30,
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["config"]["batch_size"] == 6
    assert body["config"]["interval_seconds"] == 30
    assert body["stats"]["engine"]["scenario"] == current.scenario.value

    # Real emitted events, not just the label: /step (unaffected by the
    # `running` monkeypatch -- it does not consult that property at all)
    # reads self.config.batch_size fresh, so it only generates 6 records if
    # the live update genuinely took.
    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200
    assert step_response.json()["generated"] == 6

    client.post("/api/simulate/stop")


def test_live_delivery_validation_still_takes_precedence_over_the_running_conflict(tmp_path):
    # _validate_live_delivery() runs first in start(), unchanged in
    # position by this change, so an invalid live-delivery config on a
    # running simulation still reports the pre-existing 400 (a ValueError,
    # mapped by app/exceptions.py) rather than the new 409 -- and, same as
    # the pre-existing live-delivery-only failure path, leaves no partial
    # application behind either way.
    async def scenario() -> None:
        await _start_and_settle(_config(tmp_path))
        assert controller.running is True

        with pytest.raises(ValueError, match="Live delivery requires both api_key and tenant_id"):
            await controller.start(
                _config(
                    tmp_path,
                    scenario=DAIRY,  # also, independently, a running conflict
                    delivery=DeliveryConfig(mode=DestinationMode.LIVE),  # missing api_key/tenant_id
                )
            )

        assert controller.config.scenario == LEAFY
        assert controller.engine.snapshot()["scenario"] == LEAFY.value
        assert controller.running is True

        await controller.stop()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(controller.stop())


# ---------------------------------------------------------------------------
# #217 -- run-loop crash reason is exposed in status()
# ---------------------------------------------------------------------------


def test_run_loop_crash_reason_exposed_in_status(tmp_path):
    """When _run_loop crashes, the exception type and message appear in
    status()['last_error'] and the controller stops running."""

    async def scenario():
        config = _config(tmp_path)
        await controller.start(config)
        assert controller.running is True
        assert "last_error" not in controller.status()

        # Force the run loop to crash by making step() raise.
        original_step = controller.step

        async def crashing_step(batch_size):
            raise RuntimeError("simulated engine failure")

        controller.step = crashing_step
        # Give the loop time to hit the crash.
        await asyncio.sleep(SETTLE_SECONDS)

        assert controller.running is False
        status = controller.status()
        assert "last_error" in status
        assert "RuntimeError" in status["last_error"]
        assert "simulated engine failure" in status["last_error"]

        # Restarting clears the error.
        controller.step = original_step
        await controller.start(config)
        await asyncio.sleep(SETTLE_SECONDS)
        assert controller.running is True
        assert "last_error" not in controller.status()

        await controller.stop()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(controller.stop())

"""Delivery paths hold the data-plane lock, or they do not (#208).

Every delivery path in ``SimulationController`` is snapshot -> deliver ->
commit: it reads what it needs under ``_lock``, releases the lock for the
whole network round trip, then re-acquires to write the outcome back. Two
separate properties fall out of that and both are tested here.

**The lock is genuinely free during the network call.** Not just at the end
-- during *every* chunk. ``chunk_events`` turns one delivery into
``ceil(N/500)`` sequential requests, so a 25k-record replay is ~50 of them; a
path that released the lock between the last chunk and the commit but held it
across the loop would still pin the tenant for 50 timeouts. The tests below
therefore assert on lock-hold *duration* and on how many POSTs happen while
the lock is held, not on end-state correctness: end state was always correct,
which is exactly why this contention was invisible to the existing suite.

**A batch that comes back to a store that moved on does not overwrite it.**
Releasing the lock is what creates the window: a ``reset()`` can land between
the snapshot and the commit. Each path's answer to that is asserted
per-path below, alongside the negative case -- an unrelated ``step()``, which
bumps ``revision`` but not ``store_epoch``, must *not* invalidate anything.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from app import controller as controller_module
from app import tenancy
from app.controller import (
    STALE_COMMIT_ERROR,
    STALE_DELIVERY_ABORTED_ERROR,
    SimulationController,
)
from app.delivery import DeliveryOutcome, chunk_events
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.schemas.domain import CSVImportType, DemoFixtureId
from app.schemas.integration import IntegrationConfigureRequest
from app.schemas.ingestion import CSVImportRequest, DeliveryRetryRequest, ReplayRequest
from app.schemas.simulation import SimulationConfig
from app.scenarios import ScenarioId
from app.store import EventStore


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def build_controller(tmp_path, name: str = "concurrency") -> SimulationController:
    return SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / f"{name}-events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / f"{name}-saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
    )


def config_for(controller: SimulationController, **overrides) -> SimulationConfig:
    return SimulationConfig(
        persist_path=str(controller.store.persist_path),
        interval_seconds=0.0,
        batch_size=1,
        **overrides,
    )


class ObservedLock(asyncio.Lock):
    """An ``asyncio.Lock`` that remembers how long each hold lasted.

    Substituted for ``controller._lock`` so a test can assert on hold
    *duration* rather than on whether the lock happened to be free at one
    sampled instant.
    """

    def __init__(self) -> None:
        super().__init__()
        self.holds: list[float] = []
        self._held_since: float | None = None

    async def acquire(self) -> bool:
        acquired = await super().acquire()
        self._held_since = time.perf_counter()
        return acquired

    def release(self) -> None:
        started = self._held_since
        self._held_since = None
        super().release()
        if started is not None:
            self.holds.append(time.perf_counter() - started)

    @property
    def longest_hold(self) -> float:
        return max(self.holds, default=0.0)


class DeliveryProbe:
    """Stands in for the network at ``SimulationController._deliver_payload``.

    Records, for every POST, whether ``_lock`` was held at the moment the
    "request" went out -- the thing #208 is actually about -- and can either
    burn a fixed amount of wall clock (`duration`) or park until the test
    releases it (`gate=True`), which is how a reset is made to land exactly
    mid-delivery instead of by luck.
    """

    def __init__(
        self,
        controller: SimulationController,
        *,
        duration: float = 0.0,
        gate: bool = False,
        gate_source: str | None = None,
        status: str = "posted",
    ) -> None:
        self.controller = controller
        self.duration = duration
        self.status = status
        # When set, only payloads carrying this source are parked; anything
        # else (a concurrent step, say) is answered immediately.
        self.gate_source = gate_source
        self.calls: list[int] = []
        self.lock_held_at_post: list[bool] = []
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        if not gate:
            self.release.set()

    @property
    def posts(self) -> int:
        return len(self.calls)

    @property
    def posts_under_lock(self) -> int:
        return sum(1 for held in self.lock_held_at_post if held)

    async def wait_for_a_post_in_flight(self) -> None:
        await asyncio.wait_for(self.arrived.wait(), timeout=2)

    async def __call__(self, payload, config, idempotency_key=None) -> DeliveryOutcome:
        self.calls.append(len(payload.events))
        self.lock_held_at_post.append(self.controller._lock.locked())
        self.arrived.set()
        if self.duration:
            await asyncio.sleep(self.duration)
        if self.gate_source is None or payload.source == self.gate_source:
            await asyncio.wait_for(self.release.wait(), timeout=5)
        now = datetime.now(UTC)
        return DeliveryOutcome(
            delivery_status=self.status,
            posted=len(payload.events) if self.status == "posted" else 0,
            failed=0 if self.status == "posted" else len(payload.events),
            delivery_attempts=1,
            attempted_at=now,
            completed_at=now if self.status == "posted" else None,
            error_message=None if self.status == "posted" else "probe failure",
            metadata={"idempotency_key": idempotency_key},
        )


def attach_probe(controller: SimulationController, monkeypatch, **kwargs) -> DeliveryProbe:
    probe = DeliveryProbe(controller, **kwargs)
    monkeypatch.setattr(controller, "_deliver_payload", probe)
    return probe


def use_tiny_chunks(monkeypatch, size: int = 2) -> None:
    """Chunk at `size` instead of 500.

    The controller calls ``chunk_events(events)`` positionally, so this
    exercises the real chunk loop -- the same ``for chunk in ...: await`` the
    500-cap produces on a 25k-record replay -- without staging 25k records.
    """
    monkeypatch.setattr(
        controller_module,
        "chunk_events",
        lambda events, chunk_size=size: chunk_events(events, chunk_size),
    )


SEED_LOT_HEADER = (
    "traceability_lot_code,product_description,quantity,unit_of_measure,"
    "location_name,location_gln,timestamp,field_name,"
    "immediate_subsequent_recipient,kde_reference_document,kde_packaging_hierarchy"
)


def seed_lot_csv(count: int) -> str:
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = [
        f"TLC-CONC-{index:04d},Spinach,80,cases,Valley Fresh Farms,0123456789012,"
        f"{timestamp},Field-9,Central Coast Cooler,HARV-{index:04d},case"
        for index in range(count)
    ]
    return "\n".join([SEED_LOT_HEADER, *rows]) + "\n"


async def seed_stored_records(controller: SimulationController, monkeypatch, count: int) -> None:
    """Fill the store with `count` delivered records, without a real network."""
    with monkeypatch.context() as patch:
        attach_probe(controller, patch)
        await controller.step(batch_size=count)


# --------------------------------------------------------------------------
# criterion 1 + 4: the lock is free across every chunk, measurably
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["step", "replay", "import_csv", "fixture", "retry"])
def test_no_delivery_path_posts_while_holding_the_lock(tmp_path, monkeypatch, path):
    """Criterion 1, for all five paths: zero POSTs under a held lock."""

    async def scenario() -> DeliveryProbe:
        controller = build_controller(tmp_path, name=path)
        await controller.reset(config_for(controller))
        use_tiny_chunks(monkeypatch)

        if path == "step":
            probe = attach_probe(controller, monkeypatch)
            await controller.step(batch_size=5)
        elif path == "replay":
            await seed_stored_records(controller, monkeypatch, 5)
            probe = attach_probe(controller, monkeypatch)
            await controller.replay(ReplayRequest())
        elif path == "import_csv":
            probe = attach_probe(controller, monkeypatch)
            await controller.import_csv(
                CSVImportRequest(import_type=CSVImportType.SEED_LOTS, csv_text=seed_lot_csv(5))
            )
        elif path == "fixture":
            probe = attach_probe(controller, monkeypatch)
            await controller.load_demo_fixture(DemoFixtureId.LEAFY_GREENS_TRACE)
        else:
            with monkeypatch.context() as patch:
                attach_probe(controller, patch, status="failed")
                await controller.step(batch_size=4)
            probe = attach_probe(controller, monkeypatch)
            await controller.retry_failed_delivery(DeliveryRetryRequest())
        return probe

    probe = asyncio.run(scenario())
    assert probe.posts >= 1
    assert probe.posts_under_lock == 0, f"{path} posted while holding _lock"


@pytest.mark.parametrize("path", ["replay", "import_csv"])
def test_chunked_delivery_releases_the_lock_between_every_chunk(tmp_path, monkeypatch, path):
    """Criterion 4: lock-hold duration, not end state.

    Five chunks, each burning a known amount of wall clock. If the loop ran
    inside the lock the longest single hold would cover all five; the
    assertion is that no hold covers even one, and that all five POSTs went
    out with the lock free.
    """
    chunk_delay = 0.05
    chunks = 5

    async def scenario() -> tuple[DeliveryProbe, ObservedLock, float]:
        controller = build_controller(tmp_path, name=f"hold-{path}")
        await controller.reset(config_for(controller))
        use_tiny_chunks(monkeypatch, size=1)
        if path == "replay":
            await seed_stored_records(controller, monkeypatch, chunks)

        lock = ObservedLock()
        monkeypatch.setattr(controller, "_lock", lock)
        probe = attach_probe(controller, monkeypatch, duration=chunk_delay)

        started = time.perf_counter()
        if path == "replay":
            await controller.replay(ReplayRequest())
        else:
            await controller.import_csv(
                CSVImportRequest(
                    import_type=CSVImportType.SEED_LOTS, csv_text=seed_lot_csv(chunks)
                )
            )
        return probe, lock, time.perf_counter() - started

    probe, lock, elapsed = asyncio.run(scenario())

    assert probe.posts == chunks
    assert probe.posts_under_lock == 0
    # The work really did take five sequential round trips...
    assert elapsed >= chunk_delay * chunks
    # ...and not one of them happened inside the lock.
    assert lock.longest_hold < chunk_delay, (
        f"longest _lock hold {lock.longest_hold:.3f}s covers a {chunk_delay:.3f}s round trip"
    )


# --------------------------------------------------------------------------
# criterion 2: a long replay does not block the rest of the tenant
# --------------------------------------------------------------------------


def test_long_replay_does_not_block_other_operations(tmp_path, monkeypatch):
    """Criterion 2, with the replay parked mid-delivery the whole time."""

    async def scenario() -> list[str]:
        controller = build_controller(tmp_path, name="unblocked")
        await controller.reset(config_for(controller))
        use_tiny_chunks(monkeypatch)
        await seed_stored_records(controller, monkeypatch, 6)

        probe = attach_probe(
            controller, monkeypatch, gate=True, gate_source="long-replay"
        )
        replay = asyncio.create_task(controller.replay(ReplayRequest(source="long-replay")))
        await probe.wait_for_a_post_in_flight()

        completed: list[str] = []
        # Each of these takes `_lock`. Under the old shape they would have
        # queued behind the whole replay; here they get 0.5s.
        await asyncio.wait_for(controller.step(batch_size=1), timeout=0.5)
        completed.append("step")
        await asyncio.wait_for(
            controller.configure_integration(
                IntegrationConfigureRequest(mock_friction=[])
            ),
            timeout=0.5,
        )
        completed.append("configure_integration")
        await asyncio.wait_for(
            controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER), timeout=0.5
        )
        completed.append("save_scenario")
        await asyncio.wait_for(
            controller.load_scenario_save(ScenarioId.LEAFY_GREENS_SUPPLIER), timeout=0.5
        )
        completed.append("load_scenario_save")
        await asyncio.wait_for(controller.reset(config_for(controller)), timeout=0.5)
        completed.append("reset")

        assert not replay.done(), "the replay should still be parked on the network"
        probe.release.set()
        await asyncio.wait_for(replay, timeout=2)
        return completed

    assert asyncio.run(scenario()) == [
        "step",
        "configure_integration",
        "save_scenario",
        "load_scenario_save",
        "reset",
    ]


# --------------------------------------------------------------------------
# criterion 3: what each path does when the world moved
# --------------------------------------------------------------------------


async def reset_mid_delivery(controller: SimulationController, probe: DeliveryProbe, task):
    """Land a `reset()` while `task` is parked on its first POST."""
    await probe.wait_for_a_post_in_flight()
    await asyncio.wait_for(controller.reset(config_for(controller)), timeout=1)
    probe.release.set()
    return await asyncio.wait_for(task, timeout=2)


def test_reset_during_step_discards_the_batch(tmp_path, monkeypatch):
    """step: the batch is dropped, so a cleared log stays cleared."""

    async def scenario():
        controller = build_controller(tmp_path, name="step-reset")
        await controller.reset(config_for(controller))
        probe = attach_probe(controller, monkeypatch, gate=True)
        task = asyncio.create_task(controller.step(batch_size=3))
        result = await reset_mid_delivery(controller, probe, task)
        return result, controller.store.stats()["total_records"]

    result, total = asyncio.run(scenario())
    assert total == 0, "a reset was overwritten by an in-flight step"
    assert result.error == STALE_COMMIT_ERROR
    assert result.generated == 3


def test_reset_during_import_discards_the_import(tmp_path, monkeypatch):
    """import_csv: all-or-nothing -- nothing stored, and reported as such."""

    async def scenario():
        controller = build_controller(tmp_path, name="import-reset")
        await controller.reset(config_for(controller))
        use_tiny_chunks(monkeypatch)
        probe = attach_probe(controller, monkeypatch, gate=True)
        task = asyncio.create_task(
            controller.import_csv(
                CSVImportRequest(import_type=CSVImportType.SEED_LOTS, csv_text=seed_lot_csv(6))
            )
        )
        result = await reset_mid_delivery(controller, probe, task)
        return result, controller.store.stats()["total_records"], probe.posts

    result, total, posts = asyncio.run(scenario())
    assert total == 0, "a reset was overwritten by an in-flight import"
    assert result.stored == 0
    assert result.status == "delivery_failed"
    assert result.error == STALE_COMMIT_ERROR
    # The remaining chunks were abandoned rather than sent into a log that
    # would not keep them.
    assert posts < 3


def test_reset_during_replay_abandons_the_remaining_chunks(tmp_path, monkeypatch):
    """replay: writes nothing, so the decision is where to stop sending."""

    async def scenario():
        controller = build_controller(tmp_path, name="replay-reset")
        await controller.reset(config_for(controller))
        use_tiny_chunks(monkeypatch, size=1)
        await seed_stored_records(controller, monkeypatch, 5)
        probe = attach_probe(controller, monkeypatch, gate=True)
        task = asyncio.create_task(controller.replay(ReplayRequest()))
        result = await reset_mid_delivery(controller, probe, task)
        return result, controller.store.stats()["total_records"], probe.posts

    result, total, posts = asyncio.run(scenario())
    assert posts == 1, "replay kept sending events out of a log that had been cleared"
    assert result.read == 5
    assert result.replayed == 1
    assert result.status == "failed"
    assert result.error == STALE_DELIVERY_ABORTED_ERROR
    # Replay never appends, so the reset's empty log stays empty.
    assert total == 0


def test_reset_during_fixture_load_discards_the_fixture(tmp_path, monkeypatch):
    """load_demo_fixture: a whole-log replacement, so it lands whole or not at all."""

    async def scenario():
        controller = build_controller(tmp_path, name="fixture-reset")
        await controller.reset(config_for(controller))
        probe = attach_probe(controller, monkeypatch, gate=True)
        task = asyncio.create_task(controller.load_demo_fixture(DemoFixtureId.LEAFY_GREENS_TRACE))
        result = await reset_mid_delivery(controller, probe, task)
        return result, controller.store.stats()["total_records"]

    result, total = asyncio.run(scenario())
    assert total == 0, "a reset was overwritten by an in-flight fixture load"
    assert result.stored == 0
    assert result.status == "delivery_failed"
    assert result.error == STALE_COMMIT_ERROR


def test_reset_during_retry_does_not_rewrite_the_log(tmp_path, monkeypatch):
    """retry: the write-back is skipped; records keep their failed status."""

    async def scenario():
        controller = build_controller(tmp_path, name="retry-reset")
        await controller.reset(config_for(controller))
        with monkeypatch.context() as patch:
            attach_probe(controller, patch, status="failed")
            await controller.step(batch_size=3)
        assert controller.store.stats()["total_records"] == 3

        probe = attach_probe(controller, monkeypatch, gate=True)
        task = asyncio.create_task(controller.retry_failed_delivery(DeliveryRetryRequest()))
        result = await reset_mid_delivery(controller, probe, task)
        return result, controller.store.stats()["total_records"]

    result, total = asyncio.run(scenario())
    assert total == 0, "an in-flight retry rewrote a log that had just been reset"
    assert result.status == "skipped"
    assert result.error == STALE_COMMIT_ERROR


def test_an_unrelated_step_does_not_invalidate_an_in_flight_replay(tmp_path, monkeypatch):
    """Why the guard is `store_epoch` and not `revision`.

    ``revision`` is the console's SSE change counter: every completed step
    bumps it. Guarding commits on it would mean one background step during a
    long replay threw away a batch whose store was never touched.
    """

    async def scenario():
        controller = build_controller(tmp_path, name="epoch-vs-revision")
        await controller.reset(config_for(controller))
        use_tiny_chunks(monkeypatch, size=1)
        await seed_stored_records(controller, monkeypatch, 4)

        probe = attach_probe(controller, monkeypatch, gate=True)
        revision_before = controller.revision
        epoch_before = controller.store_epoch

        replay = asyncio.create_task(controller.replay(ReplayRequest()))
        await probe.wait_for_a_post_in_flight()
        # An ordinary step lands mid-replay: it bumps `revision`, appends to
        # the log, and must leave the replay alone.
        probe.release.set()
        await asyncio.wait_for(controller.step(batch_size=1), timeout=1)
        result = await asyncio.wait_for(replay, timeout=2)
        return result, revision_before, epoch_before, controller.revision, controller.store_epoch

    result, revision_before, epoch_before, revision_after, epoch_after = asyncio.run(scenario())
    assert revision_after > revision_before, "the step should have bumped the SSE revision"
    assert epoch_after == epoch_before, "nothing replaced the log, so the epoch must not move"
    assert result.status == "posted"
    assert result.replayed == 4
    assert result.error is None


def test_step_commits_normally_when_nothing_moved(tmp_path, monkeypatch):
    """The guard must not cost the happy path anything."""

    async def scenario():
        controller = build_controller(tmp_path, name="step-happy")
        await controller.reset(config_for(controller))
        attach_probe(controller, monkeypatch)
        result = await controller.step(batch_size=3)
        return result, controller.store.stats()["total_records"]

    result, total = asyncio.run(scenario())
    assert total == 3
    assert result.error is None
    assert result.posted == 3


# --------------------------------------------------------------------------
# criterion 5: container shutdown does not inherit per-tenant stalls
# --------------------------------------------------------------------------


def test_shutdown_gives_up_on_a_wedged_delivery(tmp_path):
    """A run loop parked on the network is cancelled once the bound expires."""

    async def scenario() -> bool:
        controller = build_controller(tmp_path, name="wedged")

        async def never_returns(payload, config, idempotency_key=None):
            await asyncio.sleep(30)
            raise AssertionError("unreachable")  # pragma: no cover

        controller._deliver_payload = never_returns  # type: ignore[method-assign]
        await controller.start(config_for(controller))
        await asyncio.sleep(0)
        started = time.perf_counter()
        await controller.shutdown(timeout=0.1)
        return controller.running, time.perf_counter() - started

    running, elapsed = asyncio.run(scenario())
    assert running is False
    assert elapsed < 5, "shutdown waited on the delivery instead of bounding it"


def test_shutdown_tenant_controllers_stops_tenants_concurrently(tmp_path, monkeypatch):
    """Criterion 5: N wedged tenants cost one bound, not N of them."""
    tenant_count = 4
    stall = 0.4

    async def scenario() -> float:
        controllers = {}
        for index in range(tenant_count):
            tenant_controller = build_controller(tmp_path, name=f"tenant-{index}")

            async def stalled(payload, config, idempotency_key=None):
                await asyncio.sleep(stall)
                return DeliveryOutcome()

            tenant_controller._deliver_payload = stalled  # type: ignore[method-assign]
            await tenant_controller.start(config_for(tenant_controller))
            controllers[f"tenant-{index}"] = tenant_controller

        monkeypatch.setattr(tenancy, "_tenant_controllers", controllers)
        await asyncio.sleep(0)
        started = time.perf_counter()
        await tenancy.shutdown_tenant_controllers()
        elapsed = time.perf_counter() - started
        assert not any(item.running for item in controllers.values())
        return elapsed

    elapsed = asyncio.run(scenario())
    # Serially this would be >= tenant_count * stall; concurrently it is one.
    assert elapsed < stall * tenant_count * 0.75, (
        f"tenant shutdown took {elapsed:.2f}s for {tenant_count} tenants stalling {stall}s each"
    )


# --------------------------------------------------------------------------
# a run loop that dies must say so, and must not read as "Running"
# --------------------------------------------------------------------------


def test_a_crashing_run_loop_is_reported_instead_of_going_quiet(tmp_path, caplog):
    """A dead loop wakes the stream, records why, and stops reading as live.

    Without the handler the task simply vanished: `running` flipped to False,
    nothing was logged until asyncio collected the task, and `_revision` never
    moved -- so `/api/simulate/stream`, which blocks in `wait_for_revision`,
    kept the console showing "Running" with the last event count forever.
    """

    async def scenario():
        controller = build_controller(tmp_path, name="crash")
        await controller.reset(config_for(controller))

        def broken_add_many(records):
            raise OSError("no space left on device")

        controller.store.add_many = broken_add_many  # type: ignore[method-assign]
        controller._deliver_payload = DeliveryProbe(controller)  # type: ignore[method-assign]

        revision_before = controller.revision
        await controller.start(config_for(controller))
        deadline = time.perf_counter() + 2
        while controller.running and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)

        # A stream parked at the pre-crash revision wakes rather than hanging.
        woken = await controller.wait_for_revision(revision_before, timeout=1)
        # And a stop after the crash is still a clean no-op.
        await asyncio.wait_for(controller.stop(), timeout=1)
        return controller.running, controller.status()["stats"]["run_error"], woken

    with caplog.at_level("ERROR"):
        running, run_error, woken = asyncio.run(scenario())

    assert running is False
    assert woken > 0
    assert run_error is not None
    assert "no space left on device" in run_error
    assert "Simulation run loop stopped after an unhandled error" in caplog.text


def test_a_clean_run_clears_the_previous_run_error(tmp_path, monkeypatch):
    """`run_error` describes the *current* run, not a stale one."""

    async def scenario():
        controller = build_controller(tmp_path, name="crash-clear")
        await controller.reset(config_for(controller))
        controller._last_run_error = "Boom: from an earlier run"
        attach_probe(controller, monkeypatch)
        await controller.start(config_for(controller))
        await asyncio.sleep(0)
        run_error = controller.status()["stats"]["run_error"]
        await controller.stop()
        return run_error

    assert asyncio.run(scenario()) is None


# --------------------------------------------------------------------------
# reading the log is I/O too: it belongs off the event loop
# --------------------------------------------------------------------------


class ThreadRecorder:
    """Wraps a store method and remembers which thread ran it."""

    def __init__(self, target):
        self.target = target
        self.threads: list[str] = []

    def __call__(self, *args, **kwargs):
        import threading

        self.threads.append(threading.current_thread().name)
        return self.target(*args, **kwargs)

    @property
    def ran_off_the_loop(self) -> bool:
        return bool(self.threads) and all(name != "MainThread" for name in self.threads)


def test_reading_and_reloading_the_log_happens_off_the_event_loop(tmp_path, monkeypatch):
    """Every write path was already offloaded; the reads are the other half.

    `read_persisted_records` and `configure` both open the JSONL and run
    `model_validate_json` per line. Inline on the loop, one replay of a large
    store stalls health checks, the SSE stream and every other tenant.
    """

    async def scenario():
        controller = build_controller(tmp_path, name="offload")
        await controller.reset(config_for(controller))
        await seed_stored_records(controller, monkeypatch, 3)

        read = ThreadRecorder(controller.store.read_persisted_records)
        configure = ThreadRecorder(controller.store.configure)
        monkeypatch.setattr(controller.store, "read_persisted_records", read)
        monkeypatch.setattr(controller.store, "configure", configure)

        attach_probe(controller, monkeypatch)
        await controller.replay(ReplayRequest())
        await controller.reset(config_for(controller))
        return read, configure

    read, configure = asyncio.run(scenario())
    assert read.ran_off_the_loop, f"read_persisted_records ran on {read.threads}"
    assert configure.ran_off_the_loop, f"store.configure ran on {configure.threads}"

"""No delivery path may hold the data-plane lock across network I/O.

Regression cover for #208. All five delivery paths -- `step`, `replay`,
`import_csv`, `load_demo_fixture`, `retry_failed_delivery` -- awaited HTTP
*inside* `self._lock`. For the whole of that await, every other operation on
that tenant queued behind it: start, stop, reset, step, import, replay, retry,
configure_integration, save_scenario, load_scenario_save, load_demo_fixture.
Scaled out, a 25k-record replay against the 30s live timeout pinned the lock
for minutes.

These tests assert **contention**, not end-state correctness -- the existing
suite already covers the latter, and covered it just as well before this
change, which is exactly why the contention was invisible. Each one measures
whether a second operation can make progress while a slow delivery is in
flight.

The other half is what happens when the world moves between snapshot and
commit: a `reset()` landing mid-delivery must not have its results overwritten
by the in-flight batch. That is `_store_epoch`, and the second group of tests
covers it per path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.controller import STALE_COMMIT_NOTE, SimulationController
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.scenarios import ScenarioId
from app.schemas.domain import CTEType, DemoFixtureId, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.ingestion import CSVImportRequest, DeliveryRetryRequest, ReplayRequest
from app.schemas.scenarios import DemoFixtureLoadRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import EventStore
from tests.support.timestamps import recent_event_timestamp

DELIVERY_SECONDS = 0.4


def _controller(tmp_path: Path) -> SimulationController:
    controller = SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / "events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
        tenant_id="contention-test",
    )
    controller.config = SimulationConfig(
        persist_path=str(tmp_path / "events.jsonl"),
        batch_size=2,
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )
    return controller


def _slow_delivery(controller: SimulationController, seconds: float = DELIVERY_SECONDS):
    """Stand in for a slow endpoint, awaiting the way real delivery does."""
    original = controller._deliver_payload

    async def slow(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await original(*args, **kwargs)

    controller._deliver_payload = slow  # type: ignore[method-assign]
    return original


def _event(lot_code: str = "TLC-1") -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot_code,
        product_description="Romaine",
        quantity=10.0,
        unit_of_measure="cases",
        location_name="Valley Farms",
        timestamp="2026-02-10T08:00:00Z",
    )


def _failed_record(lot_code: str = "TLC-FAIL") -> StoredEventRecord:
    # An event the mock will ACCEPT on retry: inside its replay window, with
    # the harvesting KDEs it requires. The stale-retry test below asserts the
    # response status is "failed" because the commit was dropped (#217); an
    # event the mock rejected would read "failed" for the wrong reason and let
    # that assertion pass without the fix.
    event = _event(lot_code).model_copy(
        update={
            "timestamp": recent_event_timestamp(),
            "kdes": {"harvest_date": "2026-03-01", "reference_document": f"Harvest Log HL-{lot_code}"},
        }
    )
    return StoredEventRecord(
        payload_source="test",
        event=event,
        destination_mode=DestinationMode.MOCK,
        delivery_status="failed",
        delivery_attempts=1,
        error="boom",
    )


CSV_TEXT = (
    "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
    "location_name,timestamp\n"
    "harvesting,TLC-CSV-1,Romaine,10,cases,Valley Fresh Farms,2026-02-10T08:00:00Z\n"
)


async def _time_second_operation(controller: SimulationController, delivery, second) -> float:
    """How long a second operation waits while `delivery` is in flight."""
    delivering = asyncio.create_task(delivery())
    await asyncio.sleep(0.05)  # let the delivery reach its await
    started = time.perf_counter()
    await second()
    waited = time.perf_counter() - started
    await delivering
    return waited


# ---------------------------------------------------------------------------
# AC1/AC2 -- the lock is not held across delivery, on any of the five paths
# ---------------------------------------------------------------------------


def _assert_not_blocked(tmp_path, delivery_factory, prepare=None) -> None:
    controller = _controller(tmp_path)
    if prepare is not None:
        prepare(controller)
    _slow_delivery(controller)

    async def scenario() -> float:
        # `status()` needs no lock; `save_scenario` does, and is one of the
        # operations #208 lists as blocked for the whole delivery window.
        return await _time_second_operation(
            controller,
            delivery_factory(controller),
            lambda: controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER),
        )

    waited = asyncio.run(scenario())

    assert waited < DELIVERY_SECONDS / 2, (
        f"a second operation waited {waited:.3f}s behind a {DELIVERY_SECONDS}s delivery, "
        "so the data-plane lock is still held across it"
    )


def test_step_does_not_block_other_operations(tmp_path):
    _assert_not_blocked(tmp_path, lambda c: lambda: c.step(batch_size=2))


def test_import_csv_does_not_block_other_operations(tmp_path):
    _assert_not_blocked(
        tmp_path,
        lambda c: lambda: c.import_csv(
            CSVImportRequest(import_type="scheduled_events", csv_text=CSV_TEXT)
        ),
    )


def test_replay_does_not_block_other_operations(tmp_path):
    def prepare(controller: SimulationController) -> None:
        controller.store.add_many([StoredEventRecord(payload_source="t", event=_event())])

    _assert_not_blocked(tmp_path, lambda c: lambda: c.replay(ReplayRequest()), prepare=prepare)


def test_load_demo_fixture_does_not_block_other_operations(tmp_path):
    _assert_not_blocked(
        tmp_path,
        lambda c: lambda: c.load_demo_fixture(
            DemoFixtureId.LEAFY_GREENS_TRACE, DemoFixtureLoadRequest(reset=False)
        ),
    )


def test_retry_does_not_block_other_operations(tmp_path):
    def prepare(controller: SimulationController) -> None:
        controller.store.add_many([_failed_record()])

    _assert_not_blocked(
        tmp_path,
        lambda c: lambda: c.retry_failed_delivery(DeliveryRetryRequest()),
        prepare=prepare,
    )


def test_a_long_delivery_does_not_delay_stop(tmp_path):
    # #158's stated criterion: a stop signal must land immediately.
    controller = _controller(tmp_path)
    _slow_delivery(controller, seconds=1.0)

    async def scenario() -> float:
        await controller.start(controller.config)
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        await controller.stop()
        return time.perf_counter() - started

    assert asyncio.run(scenario()) < 1.5


# ---------------------------------------------------------------------------
# AC3 -- what happens when the store moves between snapshot and commit
# ---------------------------------------------------------------------------


async def _reset_during(controller: SimulationController, delivery):
    """Land a `reset()` while `delivery` is mid-flight, and return its result."""
    delivering = asyncio.create_task(delivery())
    await asyncio.sleep(0.05)
    await controller.reset()
    return await delivering


def test_a_reset_mid_step_is_not_overwritten_by_the_in_flight_batch(tmp_path):
    controller = _controller(tmp_path)
    _slow_delivery(controller)

    result = asyncio.run(_reset_during(controller, lambda: controller.step(batch_size=2)))

    assert controller.store.stats()["total_records"] == 0, "the reset was undone"
    assert result.error is not None and STALE_COMMIT_NOTE in result.error


def test_a_reset_mid_import_is_not_overwritten(tmp_path):
    controller = _controller(tmp_path)
    _slow_delivery(controller)

    result = asyncio.run(
        _reset_during(
            controller,
            lambda: controller.import_csv(
                CSVImportRequest(import_type="scheduled_events", csv_text=CSV_TEXT)
            ),
        )
    )

    assert controller.store.stats()["total_records"] == 0
    assert result.stored == 0, "a dropped commit must not report records as stored"
    assert result.error is not None and STALE_COMMIT_NOTE in result.error
    # #217: the drop has to be visible to a caller that only reads the
    # machine-readable status, the way the fixture load already reports it --
    # "accepted" with stored=0 and the truth buried in `error` is not that.
    assert result.status == "delivery_failed"


def test_an_undisturbed_import_still_reports_accepted(tmp_path):
    # Companion to test_an_undisturbed_delivery_still_commits: the #217
    # not-committed branch must not be firing on its own.
    controller = _controller(tmp_path)

    async def scenario():
        return await controller.import_csv(
            CSVImportRequest(import_type="scheduled_events", csv_text=CSV_TEXT)
        )

    result = asyncio.run(scenario())

    assert result.status == "accepted"
    assert result.stored == 1
    assert controller.store.stats()["total_records"] == 1
    assert result.error is None or STALE_COMMIT_NOTE not in result.error


def test_a_reset_mid_fixture_load_is_not_overwritten(tmp_path):
    controller = _controller(tmp_path)
    _slow_delivery(controller)

    result = asyncio.run(
        _reset_during(
            controller,
            lambda: controller.load_demo_fixture(
                DemoFixtureId.LEAFY_GREENS_TRACE, DemoFixtureLoadRequest(reset=False)
            ),
        )
    )

    assert controller.store.stats()["total_records"] == 0
    assert result.stored == 0
    # A load that stored nothing must not answer "loaded".
    assert result.status != "loaded"


def test_a_reset_mid_retry_does_not_resurrect_deleted_records(tmp_path):
    # The sharpest case: `update_many` rewrites the whole log, so committing it
    # after a reset would put every record the operator just deleted back.
    controller = _controller(tmp_path)
    controller.store.add_many([_failed_record(f"TLC-FAIL-{index}") for index in range(3)])
    _slow_delivery(controller)

    result = asyncio.run(
        _reset_during(controller, lambda: controller.retry_failed_delivery(DeliveryRetryRequest()))
    )

    assert controller.store.stats()["total_records"] == 0, "the retry resurrected deleted records"
    assert result.error is not None and STALE_COMMIT_NOTE in result.error
    # #217: nothing was written back, so the records this was meant to repair
    # were not repaired -- "posted" would claim otherwise.
    assert result.status == "failed"


def test_an_undisturbed_delivery_still_commits(tmp_path):
    # The epoch check must not be firing on its own.
    controller = _controller(tmp_path)

    async def scenario():
        return await controller.step(batch_size=2)

    result = asyncio.run(scenario())

    assert controller.store.stats()["total_records"] == 2
    assert result.error is None or STALE_COMMIT_NOTE not in result.error


def test_a_fixture_load_does_not_treat_its_own_reset_as_a_conflict(tmp_path):
    # `load_demo_fixture(reset=True)` clears the store itself. Reading the
    # epoch before that would make every such load drop its own commit.
    controller = _controller(tmp_path)

    async def scenario():
        return await controller.load_demo_fixture(
            DemoFixtureId.LEAFY_GREENS_TRACE, DemoFixtureLoadRequest(reset=True)
        )

    result = asyncio.run(scenario())

    assert result.status == "loaded"
    assert result.stored > 0
    assert controller.store.stats()["total_records"] == result.stored


# ---------------------------------------------------------------------------
# AC5 -- shutdown is bounded and concurrent
# ---------------------------------------------------------------------------


def test_shutdown_is_bounded_when_the_run_loop_will_not_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("REGENGINE_SHUTDOWN_TIMEOUT_SECONDS", "0.3")
    controller = _controller(tmp_path)

    async def scenario() -> float:
        async def never_stops() -> None:
            await asyncio.sleep(30)

        controller._task = asyncio.create_task(never_stops())
        started = time.perf_counter()
        await controller.shutdown()
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())

    assert elapsed < 2.0, f"shutdown waited {elapsed:.2f}s on a wedged run loop"
    assert controller._task is None


def test_tenant_shutdown_runs_concurrently(tmp_path, monkeypatch):
    from app import tenancy

    controllers = [_controller(tmp_path / f"t{index}") for index in range(4)]
    for controller in controllers:
        original = controller.stop

        async def slow_stop(_original=original):
            await asyncio.sleep(0.3)
            await _original()

        controller.stop = slow_stop  # type: ignore[method-assign]

    monkeypatch.setattr(
        tenancy,
        "_tenant_controllers",
        {f"t{index}": controller for index, controller in enumerate(controllers)},
    )

    async def scenario() -> float:
        started = time.perf_counter()
        await tenancy.shutdown_tenant_controllers()
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())

    # Serially this is 4 x 0.3s = 1.2s; concurrently it is one 0.3s wait.
    assert elapsed < 0.8, f"tenant shutdown took {elapsed:.2f}s, so it is still serial"

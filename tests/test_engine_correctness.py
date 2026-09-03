"""Regression tests for engine and controller correctness.

Covers:
  * #115 transformation fan-out — one CTE per output lot
  * #117 SSCC off-by-one truncation
  * #119 timestamp collapse at the simulated-clock future cap
  * #156 stop() orphaning a live run loop
  * #158 controller lock held across awaited delivery calls
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.controller import DeliveryOutcome, SimulationController
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.schemas.domain import CTEType
from app.schemas.simulation import SimulationConfig
from app.store import EventStore


def drain(engine: LegitFlowEngine, count: int) -> list[tuple]:
    """Generate `count` events, then flush any queued fan-out events."""
    generated = [engine.next_event() for _ in range(count)]
    while engine._pending_events:
        generated.append(engine.next_event())
    return generated


# --------------------------------------------------------------------------
# #115 — every transformation output lot gets its own TRANSFORMATION record
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["fresh_cut_processor", "seafood_first_receiver"])
def test_every_transformation_output_lot_gets_its_own_cte(scenario):
    engine = LegitFlowEngine(seed=204, scenario=scenario)
    generated = drain(engine, 400)

    transformation_lot_codes = {
        event.traceability_lot_code
        for event, _ in generated
        if event.cte_type == CTEType.TRANSFORMATION
    }
    declared_outputs: set[str] = set()
    multi_output_batches = 0
    for event, _ in generated:
        if event.cte_type != CTEType.TRANSFORMATION:
            continue
        outputs = event.kdes["output_traceability_lot_codes"]
        declared_outputs.update(outputs)
        if len(outputs) > 1:
            multi_output_batches += 1

    assert multi_output_batches > 0, "expected at least one multi-output transformation"
    missing = declared_outputs - transformation_lot_codes
    assert not missing, f"output lots without a TRANSFORMATION event: {sorted(missing)}"


def test_transformation_outputs_appear_before_they_are_shipped():
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")
    generated = drain(engine, 400)

    first_cte_by_lot: dict[str, CTEType] = {}
    transformed_lots: set[str] = set()
    for event, _ in generated:
        first_cte_by_lot.setdefault(event.traceability_lot_code, event.cte_type)
        if event.cte_type == CTEType.TRANSFORMATION:
            transformed_lots.update(event.kdes["output_traceability_lot_codes"])

    assert transformed_lots
    for lot_code in transformed_lots:
        # The first record ever stored for a transformation output must be the
        # transformation itself, never a downstream shipment that would appear
        # to link straight back to the pre-transformation inputs.
        assert first_cte_by_lot[lot_code] == CTEType.TRANSFORMATION


def test_transformation_events_carry_their_own_lot_quantity_and_inputs():
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")
    generated = drain(engine, 400)

    batches: dict[str, list] = {}
    for event, parents in generated:
        if event.cte_type == CTEType.TRANSFORMATION:
            batches.setdefault(event.kdes["reference_document_number"], []).append((event, parents))

    multi = [group for group in batches.values() if len(group) > 1]
    assert multi, "expected a fan-out batch"

    for group in multi:
        codes = [event.traceability_lot_code for event, _ in group]
        assert len(set(codes)) == len(codes)
        inputs = group[0][0].kdes["input_traceability_lot_codes"]
        for event, parents in group:
            # Each output event stands on its own lot, but shares the batch's
            # inputs so backward lineage still resolves to the input lots.
            assert event.traceability_lot_code in event.kdes["output_traceability_lot_codes"]
            assert event.kdes["output_traceability_lot_code"] == event.traceability_lot_code
            assert event.quantity > 0
            assert event.kdes["input_traceability_lot_codes"] == inputs
            assert parents == inputs
            assert event.traceability_lot_code not in parents


def test_transformation_events_do_not_share_mutable_kde_state():
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")
    generated = drain(engine, 400)
    transforms = [event for event, _ in generated if event.cte_type == CTEType.TRANSFORMATION]
    assert len(transforms) >= 2
    first, second = transforms[0], transforms[1]
    first.kdes["input_traceability_lot_codes"].append("MUTATED")
    assert "MUTATED" not in second.kdes["input_traceability_lot_codes"]


# --------------------------------------------------------------------------
# #117 — SSCC uniqueness
# --------------------------------------------------------------------------


def assert_valid_sscc(sscc: str) -> None:
    assert len(sscc) == 18
    assert sscc.isdigit()
    base = sscc[:-1]
    total = sum(
        int(digit) * (3 if index % 2 else 1)
        for index, digit in enumerate(reversed(base), start=1)
    )
    assert int(sscc[-1]) == (10 - (total % 10)) % 10


def test_sequential_ssccs_are_unique_and_well_formed():
    engine = LegitFlowEngine(seed=204, scenario="seafood_first_receiver")
    ssccs = [engine._make_sscc() for _ in range(500)]

    assert len(set(ssccs)) == len(ssccs)
    for sscc in ssccs:
        assert_valid_sscc(sscc)


def test_sscc_base_is_seventeen_digits_and_keeps_the_full_serial():
    engine = LegitFlowEngine(seed=204, scenario="seafood_first_receiver")
    for _ in range(25):
        sscc = engine._make_sscc()
        base = sscc[:-1]
        assert len(base) == 17
    # The serial's units digit must survive: consecutive serials differ.
    engine.reset(204, scenario="seafood_first_receiver")
    first = engine._make_sscc()
    second = engine._make_sscc()
    assert first[:-1] != second[:-1]


def test_shipping_references_are_unique_across_a_gs1_run():
    engine = LegitFlowEngine(seed=204, scenario="seafood_first_receiver")
    references = [
        event.kdes["reference_document_number"]
        for event, _ in drain(engine, 400)
        if event.cte_type == CTEType.SHIPPING
    ]

    assert len(references) >= 20
    assert len(set(references)) == len(references)


# --------------------------------------------------------------------------
# #119 — timestamps must not collapse at the future cap
# --------------------------------------------------------------------------


def test_consecutive_timestamps_never_collapse_even_at_the_future_cap():
    engine = LegitFlowEngine(seed=204)
    # Park the cursor on the ceiling so every following call is capped.
    engine._time_cursor = datetime.now(UTC) + timedelta(hours=20)

    stamps = [engine._advance_time(10, 240) for _ in range(200)]

    assert all(later > earlier for earlier, later in zip(stamps, stamps[1:]))
    assert max(stamps) <= datetime.now(UTC) + timedelta(hours=20, seconds=1)


def test_long_run_keeps_realistic_spacing_between_events():
    engine = LegitFlowEngine(seed=204)
    timestamps = [event.timestamp for event, _ in drain(engine, 200)]

    assert len(set(timestamps)) >= len(timestamps) - 20  # fan-out shares a batch instant
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip(timestamps, timestamps[1:])
        if later != earlier
    ]
    # A 200-event run must still read as an hours-apart chronology rather than
    # a pile of events clamped onto the live-window ceiling.
    assert sum(gaps) / len(gaps) > 600
    assert min(timestamps) >= datetime.now(UTC) - timedelta(days=90)
    assert max(timestamps) <= datetime.now(UTC) + timedelta(hours=20, seconds=1)


def test_seeded_runs_stay_reproducible():
    first = [
        (event.cte_type, event.traceability_lot_code, event.quantity)
        for event, _ in drain(LegitFlowEngine(seed=204), 150)
    ]
    second = [
        (event.cte_type, event.traceability_lot_code, event.quantity)
        for event, _ in drain(LegitFlowEngine(seed=204), 150)
    ]
    assert first == second


# --------------------------------------------------------------------------
# controller: #156 run-loop lifecycle, #158 lock scope
# --------------------------------------------------------------------------


def build_controller(tmp_path) -> SimulationController:
    persist_path = tmp_path / "engine-correctness-events.jsonl"
    return SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(persist_path)),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
    )


def live_run_loops() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and getattr(task.get_coro(), "__qualname__", "").endswith("_run_loop")
    ]


def test_racing_start_and_stop_never_orphans_a_run_loop(tmp_path):
    async def ticked_start(controller: SimulationController, config, ticks: int) -> None:
        # Shift the start() call by a number of event-loop ticks so the race
        # lands in every phase relative to stop()'s `await task`, including
        # the tick where the unlocked stop() used to clobber a fresh task.
        for _ in range(ticks):
            await asyncio.sleep(0)
        await controller.start(config)

    async def scenario() -> list[int]:
        orphan_phases: list[int] = []
        for ticks in range(12):
            controller = build_controller(tmp_path)
            config = SimulationConfig(
                interval_seconds=60,
                batch_size=1,
                seed=204,
                persist_path=str(tmp_path / "race-events.jsonl"),
            )
            await controller.start(config)
            await asyncio.gather(controller.stop(), ticked_start(controller, config, ticks))

            orphans = [task for task in live_run_loops() if task is not controller._task]
            if orphans:
                orphan_phases.append(ticks)
            await controller.stop()
            for task in live_run_loops():
                task.cancel()
            await asyncio.sleep(0)
            assert controller._task is None
            assert controller.running is False
        return orphan_phases

    assert asyncio.run(scenario()) == []


def test_stopped_run_loop_stops_generating_events(tmp_path):
    async def scenario() -> tuple[int, int]:
        controller = build_controller(tmp_path)
        config = SimulationConfig(
            interval_seconds=0.01,
            batch_size=1,
            seed=204,
            persist_path=str(tmp_path / "quiet-events.jsonl"),
        )
        await controller.start(config)
        await asyncio.sleep(0.05)
        await controller.stop()
        after_stop = controller.store.stats()["total_records"]
        await asyncio.sleep(0.1)
        return after_stop, controller.store.stats()["total_records"]

    after_stop, later = asyncio.run(scenario())
    assert after_stop == later


def test_slow_delivery_does_not_block_other_controller_calls(tmp_path):
    async def scenario() -> tuple[float, bool]:
        controller = build_controller(tmp_path)
        config = SimulationConfig(
            interval_seconds=60,
            batch_size=1,
            seed=204,
            persist_path=str(tmp_path / "slow-delivery-events.jsonl"),
        )
        released = asyncio.Event()

        async def stalled_delivery(*args, **kwargs) -> DeliveryOutcome:
            await released.wait()
            return DeliveryOutcome(delivery_status="posted", posted=1, delivery_attempts=1)

        controller._deliver_payload = stalled_delivery  # type: ignore[method-assign]
        step_task = asyncio.create_task(controller.step(batch_size=1))
        await asyncio.sleep(0.05)

        # These previously queued behind the in-flight delivery.
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(controller.start(config), timeout=1.0)
        controller.status()
        elapsed = asyncio.get_running_loop().time() - started

        released.set()
        await step_task
        await asyncio.wait_for(controller.stop(), timeout=2.0)
        return elapsed, controller.running

    elapsed, running = asyncio.run(scenario())
    assert elapsed < 0.5
    assert running is False

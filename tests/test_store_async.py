"""Tests for #136: moving EventStore's blocking file I/O off the event loop.

Two things must hold simultaneously for that fix to be correct rather than
just "not obviously broken":

- The event loop must not stall while a store write, a store read, or the
  CPU-bound CSV parse is in flight. Proven here by running a fast ticking
  coroutine concurrently with each and checking it keeps advancing --
  a coroutine that still blocked the loop synchronously (the pre-#136
  behavior) would freeze the ticker for the whole call instead.

- Moving the work onto a worker thread via asyncio.to_thread must not
  change EventStore's write-flush-commit failure semantics (#185: a
  failed write leaves no phantom record and does not advance the
  counter), and must not weaken the mutual exclusion its threading.RLock
  provides -- now genuinely exercised across OS threads instead of only
  across asyncio tasks sharing one thread.

See the locking comment beside ``EventStore._lock`` (app/store.py) and
above ``SimulationController._store_add_many`` (app/controller.py) for the
argument these tests check empirically. Per the task's file-ownership
rules this file only drives the public controller API and the new
``SimulationController._store_*`` helpers -- it never edits an existing
test file and never touches ``EventStore``'s own (unchanged) behavior,
which ``tests/test_store_durability.py`` and ``tests/test_store.py``
already cover directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable

import pytest

import app.controller as controller_module
from app.controller import SimulationController
from app.csv_importer import ParsedCSVImport
from app.engine import LegitFlowEngine
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.schemas.domain import (
    CSVImportType,
    CTEType,
    DestinationMode,
    RegEngineEvent,
    StoredEventRecord,
)
from app.schemas.ingestion import CSVImportRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import EventStore

BASE_TIME = datetime(2026, 2, 5, 8, 30, tzinfo=UTC)

# Tuning for the "did the loop stay responsive" tests below: a background
# ticker advances every TICK_INTERVAL seconds while a deliberately slowed
# store/parse call is in flight for SLOW_CALL_SECONDS. If the event loop
# is genuinely free during that call, the ticker keeps advancing for
# (SLOW_CALL_SECONDS / TICK_INTERVAL) more ticks; if the call still
# blocks the loop, it freezes for the entire window and advances zero.
# MIN_EXTRA_TICKS sits well below what a free loop achieves, to stay
# robust on slow/loaded CI, while remaining far above the 0 a blocked
# loop would produce -- so the assertion still cleanly distinguishes the
# two cases.
TICK_INTERVAL = 0.02
WARMUP_SECONDS = 0.05
SLOW_CALL_SECONDS = 0.25
MIN_EXTRA_TICKS = 5


def make_record(lot_code: str, kdes: dict | None = None) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=BASE_TIME,
            kdes=kdes or {},
        ),
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


def _build_controller(tmp_path: Path) -> SimulationController:
    """A standalone controller wired to its own tmp_path store.

    Deliberately not the app.main singleton: these tests monkeypatch
    EventStore.add_many / parse_csv_import process-wide for the duration
    of a single test and fire off concurrent store writes, so sharing the
    app's global controller would risk bleeding into (or racing) other
    test modules. delivery.mode=NONE keeps _deliver_payload a no-op so
    each test exercises the store/parse offload in isolation, not mock
    delivery.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    controller = SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=store,
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "scenario_saves")),
        mock_service=MockRegEngineService(store=store),
        live_client=LiveRegEngineClient(),
    )
    controller.config = SimulationConfig(
        persist_path=str(persist_path),
        delivery=DeliveryConfig(mode=DestinationMode.NONE),
    )
    return controller


async def _ticks_during(awaitable: Awaitable[Any]) -> tuple[int, int]:
    """Run a background ticker while awaiting ``awaitable``.

    Returns ``(ticks_before, ticks_after)`` so a caller can assert the
    ticker kept advancing *during* the await -- proof the awaited call
    never blocked the event loop thread. See the module docstring.
    """
    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(TICK_INTERVAL)

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(WARMUP_SECONDS)
    before = ticks

    await awaitable

    after = ticks
    stop = True
    await ticker_task
    return before, after


# ---------------------------------------------------------------------------
# The event loop must stay responsive during a slow store write / CSV parse.
# ---------------------------------------------------------------------------


def test_step_offloads_the_store_write_so_the_event_loop_stays_responsive(tmp_path, monkeypatch):
    """Reproduces #136's actual complaint for the ``step`` call site: a
    slow EventStore.add_many must not stall unrelated concurrent
    coroutines. Patches add_many to sleep, simulating a slow disk, and
    checks a concurrent ticker keeps advancing while ``step`` awaits it.
    """
    controller = _build_controller(tmp_path)
    real_add_many = EventStore.add_many

    def slow_add_many(self: EventStore, records: Any) -> list[StoredEventRecord]:
        time.sleep(SLOW_CALL_SECONDS)
        return real_add_many(self, records)

    monkeypatch.setattr(EventStore, "add_many", slow_add_many)

    before, after = asyncio.run(_ticks_during(controller.step(batch_size=2)))

    assert after - before >= MIN_EXTRA_TICKS, (
        "the ticker barely advanced while add_many() was 'writing' -- "
        "the event loop was blocked by the store write"
    )
    # The (slow) write still actually happened and stored the batch --
    # offloading it must not have dropped or truncated it.
    assert len(controller.store.recent()) == 2


def test_import_csv_offloads_the_cpu_bound_parse_so_the_event_loop_stays_responsive(
    tmp_path, monkeypatch
):
    """Same proof as above for the CPU-bound parse_csv_import() call
    (#136's proposed fix names this explicitly, alongside the store
    writes). Patches it to sleep and checks the loop stays free.
    """
    controller = _build_controller(tmp_path)

    def slow_parse(
        import_type: CSVImportType, csv_text: str, default_timestamp: datetime | None = None
    ) -> ParsedCSVImport:
        time.sleep(SLOW_CALL_SECONDS)
        return ParsedCSVImport(total=0, events=[], parent_lot_codes=[], errors=[], warnings=[])

    monkeypatch.setattr(controller_module, "parse_csv_import", slow_parse)

    request = CSVImportRequest(import_type=CSVImportType.SCHEDULED_EVENTS, csv_text="unused\n")
    before, after = asyncio.run(_ticks_during(controller.import_csv(request)))

    assert after - before >= MIN_EXTRA_TICKS, (
        "the ticker barely advanced while parse_csv_import() was 'parsing' -- "
        "the event loop was blocked by the CPU-bound parse"
    )


# ---------------------------------------------------------------------------
# #185's write-flush-commit failure semantics must survive the #136 offload.
# ---------------------------------------------------------------------------


def test_add_many_async_path_preserves_write_flush_commit_failure_semantics(tmp_path):
    """The async path must fail exactly like the direct synchronous call
    already proven in tests/test_store_durability.py: a disk write that
    fails must leave no phantom record visible via recent() and must not
    advance the counter, whether the failure happens on the event loop
    thread or -- as it now does -- on an asyncio.to_thread worker thread.
    """
    controller = _build_controller(tmp_path)
    good_persist_path = controller.store.persist_path

    async def scenario() -> list[StoredEventRecord]:
        await controller._store_add_many([make_record("TLC-BASELINE-000001")])

        controller.store.persist_path = Path("/dev/full")
        with pytest.raises(OSError):
            await controller._store_add_many([make_record("TLC-PHANTOM-000002")])
        controller.store.persist_path = good_persist_path

        # A follow-up call must continue at sequence 2, not 3 -- proving
        # the counter genuinely wasn't bumped by the failed write, not
        # just that recent() happens to hide it.
        return await controller._store_add_many([make_record("TLC-RESUMED-000002")])

    resumed = asyncio.run(scenario())

    assert resumed[0].sequence_no == 2
    assert [record.event.traceability_lot_code for record in controller.store.recent()] == [
        "TLC-RESUMED-000002",
        "TLC-BASELINE-000001",
    ]
    assert controller.store._counter == 2

    # And the phantom really never reached disk -- memory and disk agree.
    on_disk_lot_codes = [
        json.loads(line)["event"]["traceability_lot_code"]
        for line in good_persist_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert on_disk_lot_codes == ["TLC-BASELINE-000001", "TLC-RESUMED-000002"]


# ---------------------------------------------------------------------------
# The RLock must still serialize concurrent writers once they run on
# genuinely different OS threads, not just different asyncio tasks.
# ---------------------------------------------------------------------------


def test_concurrent_async_writes_to_the_same_store_stay_mutually_exclusive(tmp_path):
    """#136's core locking risk, made concrete: dispatching add_many via
    asyncio.to_thread means concurrent callers now genuinely run on
    different worker threads rather than interleaving cooperatively on
    one. If that broke the store's RLock, it would show up here as
    duplicate or skipped sequence numbers, a lost write, or a corrupted
    (unparseable) line from two writers interleaving mid-write. None of
    that may happen: the lock must serialize them exactly as it does for
    same-thread callers today.
    """
    controller = _build_controller(tmp_path)
    concurrency = 30

    async def scenario() -> None:
        await asyncio.gather(
            *[
                controller._store_add_many([make_record(f"TLC-CONC-{index:06d}")])
                for index in range(concurrency)
            ]
        )

    asyncio.run(scenario())

    assert controller.store._counter == concurrency

    on_disk_lines = [
        line
        for line in controller.store.persist_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(on_disk_lines) == concurrency, "a concurrent write was lost or duplicated on disk"

    # json.loads raising here would itself mean two writers tore a line.
    parsed_lines = [json.loads(line) for line in on_disk_lines]
    sequence_numbers = sorted(entry["sequence_no"] for entry in parsed_lines)
    assert sequence_numbers == list(range(1, concurrency + 1)), (
        "sequence numbers were not a gapless 1..N run -- concurrent writers were not serialized"
    )

    lot_codes_on_disk = {entry["event"]["traceability_lot_code"] for entry in parsed_lines}
    assert lot_codes_on_disk == {f"TLC-CONC-{index:06d}" for index in range(concurrency)}

    # recent() must agree with disk: every accepted write is visible.
    in_memory_lot_codes = {record.event.traceability_lot_code for record in controller.store.recent(limit=100)}
    assert in_memory_lot_codes == lot_codes_on_disk


# ---------------------------------------------------------------------------
# End-to-end sanity: the offloaded read path (replay -> read_persisted_
# records) still returns correct data through the public API.
# ---------------------------------------------------------------------------


def test_replay_uses_the_offloaded_read_and_returns_the_persisted_records(tmp_path):
    """replay() now awaits ``_store_read_persisted_records`` instead of
    calling ``store.read_persisted_records`` directly. This checks the
    wiring end-to-end: the records handed back through the worker thread
    must be exactly what was persisted, not empty or truncated.
    """
    controller = _build_controller(tmp_path)

    async def scenario():
        await controller._store_add_many([make_record("TLC-REPLAY-A"), make_record("TLC-REPLAY-B")])
        return await controller.replay()

    result = asyncio.run(scenario())

    assert result.read == 2
    assert result.replayed == 2
    # DestinationMode.NONE -> DeliveryOutcome.delivery_status == "generated"
    # -> replay's own status mapping -> "rebuilt".
    assert result.status == "rebuilt"

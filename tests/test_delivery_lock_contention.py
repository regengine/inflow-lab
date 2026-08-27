"""Coverage for #208: every delivery path used to await the network inside
the controller's data-plane lock.

#158 fixed the *signalling* half (``stop()`` moved to ``_lifecycle_lock``).
This file covers the data-plane half: ``step``, ``replay``, ``import_csv``,
``load_demo_fixture`` and ``retry_failed_delivery`` snapshot under
``self._lock``, POST with it RELEASED -- every chunk of a chunked delivery,
not just the first -- and re-acquire only to commit.

Why the existing suite could not see this
-------------------------------------------------------------------------
``tests/test_stop_start_race.py`` substitutes a scripted stand-in for the
real ``_run_loop`` and a no-op for ``_store_configure`` (deliberately, and
for good reasons of its own -- it is pinning a *task lifecycle* race and
must not depend on real delivery timing). Nothing in it ever reaches
``_deliver_payload``, so the contention this file is about was invisible
to it. Everything below drives the real methods with a stubbed
``live_client``, which is the only layer replaced.

What is asserted, per #208's acceptance criteria
-------------------------------------------------------------------------
1. No delivery path holds ``self._lock`` across an awaited POST -- checked
   at the moment of every POST, including each chunk of a multi-chunk
   replay (``test_no_delivery_path_posts_while_holding_the_data_plane_lock``,
   ``test_every_chunk_of_a_chunked_replay_posts_with_the_lock_released``).
4. Measured rather than inferred: the longest single hold of ``self._lock``
   during a three-chunk replay is shorter than one POST, and the number of
   POSTs observed under any one hold is zero
   (``test_the_longest_lock_hold_is_shorter_than_a_single_post``). This is
   the A/B the issue reports as 4-POSTs-per-hold on the stack vs 1 on main.
2. A replay stalled on a hung endpoint does not block ``step``, ``reset``,
   ``configure_integration``, ``save_scenario`` or ``load_scenario_save``
   (``test_a_stalled_replay_blocks_none_of_the_other_controller_paths``).
3. The atomicity decision each path now makes explicitly, one test per
   path: a ``reset()`` (or scenario load, or a persist_path repoint)
   landing between snapshot and commit wins, and the in-flight batch's
   records are dropped rather than written into the store the operator
   just cleared. Plus the negative control -- an ordinary ``start()`` on
   the *same* persist_path must NOT discard anything.

Kept in its own file: parallel workstreams own existing test files, and
these all drive ``app.main.controller`` (the shared singleton the other
controller-facing suites use) directly rather than through ``TestClient``.
"""

from __future__ import annotations

import asyncio
import time

from app.controller import STALE_COMMIT_MESSAGE
from app.main import controller
from app.mock_service import MAX_BATCH_EVENTS
from app.regengine_client import LiveIngestResult
from app.schemas.domain import (
    CSVImportType,
    CTEType,
    DemoFixtureId,
    DestinationMode,
    RegEngineEvent,
    StoredEventRecord,
)
from app.schemas.ingestion import CSVImportRequest, DeliveryRetryRequest, ReplayRequest
from app.schemas.scenarios import DemoFixtureLoadRequest, ScenarioSaveRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.scenarios import ScenarioId
from app.schemas.integration import IntegrationConfigureRequest


def _live_config(**overrides) -> SimulationConfig:
    """A config whose delivery mode is LIVE, so every POST goes through the
    stubbed ``live_client`` below rather than the in-process mock service.

    Live is the right mode for this file specifically because
    ``_deliver_payload``'s live branch is the only one that actually
    ``await``s -- the mock branch calls ``mock_service.ingest`` synchronously,
    so it could never demonstrate a lock held across a suspension point.
    """
    return SimulationConfig(
        delivery=DeliveryConfig(mode=DestinationMode.LIVE, api_key="k", tenant_id="t"),
        **overrides,
    )


# The genuine LiveRegEngineClient the shared controller is built with.
# Captured once, at import, before any test has had a chance to swap in a
# stub -- so teardown can prove a test put the real one back rather than
# merely assuming it.
_REAL_LIVE_CLIENT = controller.live_client


def _rebind_controller_locks() -> None:
    """Hand the shared controller a fresh set of unbound asyncio locks.

    An ``asyncio.Lock`` binds itself to a running loop the first time it is
    actually *contended* -- the uncontended fast path in ``acquire()``
    returns without ever calling ``_get_loop()``, which is why the rest of
    the suite can share one module-level controller across many
    ``asyncio.run`` calls without noticing. This file deliberately creates
    that contention (parking a delivery in flight while other calls run),
    so without this the singleton would be left bound to a loop that
    ``asyncio.run`` has already closed, and the next test in *any* file to
    contend on it would die with "bound to a different event loop".

    Replacing the locks between tests is safe precisely because each test
    here starts from a quiesced controller: setup_function's reset has
    already stopped any run loop, so nothing is holding them.
    """
    controller._lock = asyncio.Lock()
    controller._retry_lock = asyncio.Lock()
    controller._lifecycle_lock = asyncio.Lock()
    controller._change_condition = asyncio.Condition()


def setup_function() -> None:
    # app/main.py's `controller` is a module-level singleton shared by every
    # test file in the process (same convention as tests/test_api.py and
    # tests/test_delivery_batching.py) -- reset it before each test so
    # records left behind elsewhere cannot leak in.
    asyncio.run(controller.reset(SimulationConfig()))
    _rebind_controller_locks()


def teardown_function() -> None:
    """Restore the shared singleton, then prove the test had already done so.

    Every test here swaps something onto the module-level controller (a
    stub ``live_client``, and in one case a probe wrapped around
    ``_lock``) and restores it in a ``finally``. Teardown restoring them
    again would silently paper over a test that forgot -- and
    ``_rebind_controller_locks`` in particular would replace a leaked probe
    with a fresh Lock, hiding the leak completely while the *next* test ran
    against contaminated instrumentation.

    So: capture what the test actually left behind, put the singleton back
    to pristine unconditionally, and only then assert the captured state
    was already clean. The next test starts clean either way, and a leak is
    attributed to the test that caused it instead of surfacing as a
    mysterious failure somewhere downstream.
    """
    leaked_lock = controller._lock
    leaked_client = controller.live_client

    controller.live_client = _REAL_LIVE_CLIENT
    asyncio.run(controller.reset(SimulationConfig()))
    _rebind_controller_locks()

    assert isinstance(leaked_lock, asyncio.Lock), (
        f"test left {type(leaked_lock).__name__} wrapped around controller._lock "
        "instead of restoring the real asyncio.Lock"
    )
    assert leaked_client is _REAL_LIVE_CLIENT, (
        f"test left {type(leaked_client).__name__} installed as "
        "controller.live_client instead of restoring the real one"
    )
    assert not controller.running, "test left a live run loop on the shared controller"


async def _idle_run_loop(stop_event: asyncio.Event) -> None:
    """A ``_run_loop`` stand-in that idles until stopped.

    The two ``start()`` tests below need ``start()`` driven for real -- its
    ``_store_configure`` call is the thing under test -- but not the loop it
    spawns: the real one immediately calls ``step()``, which would contend
    for ``self._lock`` with the very commit those tests have parked and add
    a second delivery's records to the store. Same substitution
    tests/test_stop_start_race.py makes, for the same reason: what is under
    test is ``start()``'s own body, not the loop's.
    """
    await stop_event.wait()


def _accepted_response(payload, idempotency_key):
    return LiveIngestResult(
        response={
            "accepted": len(payload.events),
            "rejected": 0,
            "events": [
                {
                    "traceability_lot_code": event.traceability_lot_code,
                    "cte_type": event.cte_type.value,
                    "status": "accepted",
                }
                for event in payload.events
            ],
        },
        metadata={"delivery_mode": "live", "idempotency_key": idempotency_key, "status_code": 200},
    )


class ProbingLiveClient:
    """Stands in for LiveRegEngineClient and records the lock state per POST.

    ``controller._lock.locked()`` is sampled at the top of every ``ingest``
    call -- i.e. exactly at the network await #208 is about. ``delay``
    makes each POST take real (if small) wall-clock time so a hold that
    spans one is measurably longer than one that does not.

    ``gate`` suspends only the FIRST POST, deliberately: the tests that use
    it want one delivery parked in flight while other controller calls run,
    and those other calls must be free to deliver normally -- a gate that
    caught every POST would hang the very ``step()`` whose promptness is
    the thing under test.
    """

    def __init__(self, *, delay: float = 0.0, gate: asyncio.Event | None = None) -> None:
        self.delay = delay
        self.gate = gate
        self.locked_during_post: list[bool] = []
        self.payload_sizes: list[int] = []
        # (start, end) monotonic bounds of each POST -- see
        # test_the_longest_lock_hold_never_overlaps_a_post for why the
        # interval, rather than a duration, is what gets asserted on.
        self.post_intervals: list[tuple[float, float]] = []
        self.started = asyncio.Event()
        self._gate_consumed = False

    async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
        self.locked_during_post.append(controller._lock.locked())
        self.payload_sizes.append(len(payload.events))
        gated = self.gate is not None and not self._gate_consumed
        if gated:
            self._gate_consumed = True
        self.started.set()
        started_at = time.monotonic()
        if gated:
            await self.gate.wait()
        elif self.delay:
            await asyncio.sleep(self.delay)
        else:
            await asyncio.sleep(0)
        self.post_intervals.append((started_at, time.monotonic()))
        return _accepted_response(payload, idempotency_key)


def _event(lot_code: str, *, kdes: dict | None = None) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot_code,
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp="2026-03-01T08:00:00Z",
        kdes={"harvest_date": "2026-03-01", "reference_document": f"HL-{lot_code}"}
        if kdes is None
        else kdes,
    )


def _seed_lot_csv(lot_codes: list[str]) -> str:
    header = (
        "traceability_lot_code,product_description,quantity,"
        "unit_of_measure,location_name,reference_document"
    )
    rows = [f"{lot},Romaine Hearts,10,cases,Valley Fresh Farms,HL-{lot}" for lot in lot_codes]
    return "\n".join([header, *rows]) + "\n"


def _seed_store(count: int, *, prefix: str, delivery_status: str = "posted") -> list[StoredEventRecord]:
    records = [
        StoredEventRecord(
            payload_source="lock-contention-suite",
            event=_event(f"{prefix}-{index}"),
            destination_mode=DestinationMode.LIVE,
            delivery_status=delivery_status,
            delivery_attempts=1,
            error="connection reset" if delivery_status == "failed" else None,
            delivery_response=None,
            delivery_metadata={"delivery_mode": "live"},
        )
        for index in range(count)
    ]
    controller.store.add_many(records)
    return records


# ---------------------------------------------------------------------------
# Criterion 1 -- no delivery path POSTs while holding self._lock.
# ---------------------------------------------------------------------------


def _run_every_delivery_path(client: ProbingLiveClient) -> None:
    """Drive all five paths named in #208's table against *client*."""

    async def scenario() -> None:
        controller.live_client = client

        await controller.step(batch_size=2)

        _seed_store(3, prefix="TLC-REPLAY")
        await controller.replay(ReplayRequest())

        await controller.import_csv(
            CSVImportRequest(
                import_type=CSVImportType.SEED_LOTS,
                csv_text=_seed_lot_csv(["TLC-IMPORT-A", "TLC-IMPORT-B"]),
            )
        )

        await controller.load_demo_fixture(
            DemoFixtureId.LEAFY_GREENS_TRACE, DemoFixtureLoadRequest(reset=False)
        )

        _seed_store(2, prefix="TLC-RETRY", delivery_status="failed")
        await controller.retry_failed_delivery(DeliveryRetryRequest())

    asyncio.run(controller.reset(_live_config()))
    original_client = controller.live_client
    try:
        asyncio.run(scenario())
    finally:
        controller.live_client = original_client


def test_no_delivery_path_posts_while_holding_the_data_plane_lock():
    """#208 criterion 1, for all five paths in one pass.

    Verified to fail before the fix: with the delivery awaits still inside
    ``async with self._lock``, every sampled value was True (one per POST
    across step/replay/import_csv/load_demo_fixture/retry). After the fix
    every sample is False.
    """
    client = ProbingLiveClient()
    _run_every_delivery_path(client)

    assert client.locked_during_post, "no delivery reached the live client at all"
    assert not any(client.locked_during_post), (
        "a delivery path awaited the network while holding self._lock: "
        f"{client.locked_during_post}"
    )


def test_every_chunk_of_a_chunked_replay_posts_with_the_lock_released():
    """#208 criterion 1's "including every chunk of a chunked delivery".

    ``_deliver_in_chunks`` (added by #103) turns one lock-held call into
    ``ceil(N/MAX_BATCH_EVENTS)`` sequential ones, which is what took the
    issue's measurement from 1 POST per hold to 4. Three chunks here, and
    all three must be posted with the lock released -- a fix that only
    hoisted the *first* await out of the lock would still show True for
    chunks two and three.
    """
    client = ProbingLiveClient()

    async def scenario() -> None:
        controller.live_client = client
        await controller.replay(ReplayRequest())

    asyncio.run(controller.reset(_live_config()))
    _seed_store(MAX_BATCH_EVENTS * 2 + 5, prefix="TLC-CHUNK")
    original_client = controller.live_client
    try:
        asyncio.run(scenario())
    finally:
        controller.live_client = original_client

    assert len(client.payload_sizes) == 3, client.payload_sizes
    assert client.payload_sizes == [MAX_BATCH_EVENTS, MAX_BATCH_EVENTS, 5]
    assert client.locked_during_post == [False, False, False]


# ---------------------------------------------------------------------------
# Criterion 4 -- measured lock-hold duration / POSTs under one hold.
# ---------------------------------------------------------------------------


class LockHoldProbe:
    """Wraps ``controller._lock`` and times every hold of it.

    An ``asyncio.Lock`` is exclusive, so at most one holder exists at a
    time and a single start timestamp is enough. ``locked()`` is delegated
    so ProbingLiveClient's own per-POST sample keeps working through it.
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        # (start, end) monotonic bounds of each hold of self._lock.
        self.hold_intervals: list[tuple[float, float]] = []
        self.posts_under_current_hold = 0
        self.max_posts_under_one_hold = 0
        self._started_at: float | None = None

    @property
    def hold_durations(self) -> list[float]:
        return [end - start for start, end in self.hold_intervals]

    def locked(self) -> bool:
        return self._lock.locked()

    def note_post(self) -> None:
        if self._started_at is not None:
            self.posts_under_current_hold += 1
            self.max_posts_under_one_hold = max(
                self.max_posts_under_one_hold, self.posts_under_current_hold
            )

    async def acquire(self) -> bool:
        await self._lock.acquire()
        self._started_at = time.monotonic()
        self.posts_under_current_hold = 0
        return True

    def release(self) -> None:
        if self._started_at is not None:
            self.hold_intervals.append((self._started_at, time.monotonic()))
            self._started_at = None
        self._lock.release()

    async def __aenter__(self) -> "LockHoldProbe":
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.release()


def test_the_longest_lock_hold_never_overlaps_a_post():
    """#208 criterion 4: assert the hold, not just the end state.

    Three chunks x a 60ms POST. Before the fix the whole replay ran under
    one hold, so that hold's interval contained all three POSTs and
    ``max_posts_under_one_hold`` was 3 -- the issue's own "4 POSTs under one
    hold" measurement, at chunk-count 3. After the fix no lock hold and no
    POST overlap at all.

    What is asserted, and why it is an interval overlap rather than a
    duration
    ---------------------------------------------------------------------
    The first version of this test asserted ``max(hold_durations) <
    post_delay`` -- an absolute wall-clock threshold. That was wrong, and
    deterministically so: it passed alone (~14ms) and failed whenever its
    siblings ran first (~64ms), with the store state provably identical
    both ways (1001 records seeded, three chunks of 500/500/1, 1001 lines
    on disk, and the same count of live StoredEventRecord objects). The
    difference was a gen-2 GC pause landing inside the measured window --
    the snapshot phase reads and validates 1001 pydantic models, which
    allocates heavily, so whether a collection falls inside the hold
    depends on how much allocation happened earlier in the process. A
    threshold over real work is contaminated by allocation history,
    machine speed and heap size, none of which this test is about.

    The property #208 actually needs is causal, not statistical: a lock
    hold must never be in progress while a POST is in progress. Recording
    both as (start, end) intervals and asserting no pair overlaps says
    exactly that, at any speed, with no constant to tune -- and it is
    strictly STRONGER than the threshold it replaces. It catches a hold
    spanning even a microsecond-long POST, which no duration bound can,
    and it catches a hold that merely *begins* mid-POST, which
    ``max_posts_under_one_hold`` alone would miss because that counter only
    sees POSTs that start while a hold is already open.
    """

    def _overlap(first: tuple[float, float], second: tuple[float, float]) -> float:
        return min(first[1], second[1]) - max(first[0], second[0])

    post_delay = 0.06
    client = ProbingLiveClient(delay=post_delay)
    probe = LockHoldProbe(controller._lock)
    client_note_post = client.locked_during_post

    async def scenario() -> None:
        controller.live_client = client
        await controller.replay(ReplayRequest())

    asyncio.run(controller.reset(_live_config()))
    _seed_store(MAX_BATCH_EVENTS * 2 + 1, prefix="TLC-HOLD")

    original_client = controller.live_client
    original_lock = controller._lock

    # ProbingLiveClient samples controller._lock.locked(); route its
    # per-POST notification through the probe as well so "POSTs under one
    # hold" is counted from inside the same hold being timed.
    original_ingest = client.ingest

    async def counting_ingest(payload, config, idempotency_key=None):  # noqa: ANN001
        probe.note_post()
        return await original_ingest(payload, config, idempotency_key=idempotency_key)

    client.ingest = counting_ingest  # type: ignore[method-assign]
    controller._lock = probe  # type: ignore[assignment]
    try:
        started = time.monotonic()
        asyncio.run(scenario())
        elapsed = time.monotonic() - started
    finally:
        controller._lock = original_lock
        controller.live_client = original_client

    assert len(client_note_post) == 3, client_note_post
    # The work really did take three sequential POSTs, each of real
    # duration -- otherwise there would be no window for a hold to overlap
    # and the assertions below would pass trivially. A lower bound on
    # elapsed time, so a GC pause can only ever help it hold.
    assert elapsed >= post_delay * 3
    assert len(client.post_intervals) == 3
    assert all(end - start >= post_delay for start, end in client.post_intervals)

    assert probe.max_posts_under_one_hold == 0, (
        "a POST was awaited while self._lock was held "
        f"({probe.max_posts_under_one_hold} under one hold)"
    )
    assert probe.hold_intervals, "self._lock was never taken at all"

    overlaps = [
        (hold, post, _overlap(hold, post))
        for hold in probe.hold_intervals
        for post in client.post_intervals
        if _overlap(hold, post) > 0
    ]
    assert not overlaps, (
        "self._lock was held while a POST was in flight; longest overlap "
        f"{max(overlap for _, _, overlap in overlaps):.3f}s across "
        f"{len(overlaps)} hold/POST pair(s). Holds: "
        f"{[f'{d:.3f}s' for d in probe.hold_durations]}"
    )


# ---------------------------------------------------------------------------
# Criterion 2 -- a stalled delivery blocks no other controller path.
# ---------------------------------------------------------------------------


def test_a_stalled_replay_blocks_none_of_the_other_controller_paths():
    """#208 criterion 2, named path by named path.

    The replay's POST hangs on an event this test controls -- the stand-in
    for the issue's unresponsive endpoint at the 30s live timeout. While it
    hangs, every operation the issue lists as blocked for that tenant must
    still complete. Before the fix each of these deadlocked until the
    replay finished, because all of them take ``self._lock``.
    """
    gate = asyncio.Event()
    client = ProbingLiveClient(gate=gate)
    completed: list[str] = []

    async def scenario() -> None:
        controller.live_client = client
        replay_task = asyncio.create_task(controller.replay(ReplayRequest()))
        # Wait until the replay is genuinely suspended inside the POST.
        await asyncio.wait_for(client.started.wait(), timeout=5)
        assert not replay_task.done()

        async def with_timeout(name: str, coro) -> None:
            await asyncio.wait_for(coro, timeout=5)
            completed.append(name)

        await with_timeout(
            "configure_integration",
            controller.configure_integration(IntegrationConfigureRequest(mock_friction=[])),
        )
        await with_timeout("save_scenario", controller.save_scenario(ScenarioId.LEAFY_GREENS_SUPPLIER))
        await with_timeout("step", controller.step(batch_size=1))
        await with_timeout("reset", controller.reset(_live_config()))
        await with_timeout(
            "load_scenario_save", controller.load_scenario_save(ScenarioId.LEAFY_GREENS_SUPPLIER)
        )

        gate.set()
        await asyncio.wait_for(replay_task, timeout=5)

    asyncio.run(controller.reset(_live_config()))
    _seed_store(2, prefix="TLC-STALL")
    original_client = controller.live_client
    try:
        asyncio.run(scenario())
    finally:
        controller.live_client = original_client

    assert completed == [
        "configure_integration",
        "save_scenario",
        "step",
        "reset",
        "load_scenario_save",
    ]


# ---------------------------------------------------------------------------
# Criterion 3 -- what each path does when the world moved underneath it.
# ---------------------------------------------------------------------------


def _gated_run(seed, action, interrupt):
    """Start *action*, let it reach its POST, run *interrupt*, then release.

    Returns ``action``'s result. This is the deterministic shape of "a
    reset landed mid-delivery": the interrupt runs while the delivery is
    provably suspended, never on a hoped-for timing window.
    """
    gate = asyncio.Event()
    client = ProbingLiveClient(gate=gate)
    result: dict[str, object] = {}

    async def scenario() -> None:
        controller.live_client = client
        seed()
        task = asyncio.create_task(action())
        await asyncio.wait_for(client.started.wait(), timeout=5)
        assert not task.done()
        await asyncio.wait_for(interrupt(), timeout=5)
        gate.set()
        result["value"] = await asyncio.wait_for(task, timeout=5)

    asyncio.run(controller.reset(_live_config()))
    original_client = controller.live_client
    try:
        asyncio.run(scenario())
    finally:
        controller.live_client = original_client
    return result["value"]


def test_a_reset_landing_mid_step_is_not_overwritten_by_the_in_flight_batch():
    """#208 criterion 3's named case, for ``step``.

    Before the fix this could not happen at all (reset queued behind the
    step's lock). With the lock released around the POST it can, and the
    reset must win: the step's records are dropped, the store stays empty,
    and the response says so rather than reporting a silent success.
    """
    response = _gated_run(
        seed=lambda: None,
        action=lambda: controller.step(batch_size=2),
        interrupt=lambda: controller.reset(_live_config()),
    )

    assert controller.store.all_between() == []
    assert response.generated == 2
    assert response.posted == 2  # the POST really did happen; don't under-report it
    assert STALE_COMMIT_MESSAGE in (response.error or "")


def test_a_reset_landing_mid_import_is_not_overwritten_by_the_in_flight_batch():
    response = _gated_run(
        seed=lambda: None,
        action=lambda: controller.import_csv(
            CSVImportRequest(
                import_type=CSVImportType.SEED_LOTS,
                csv_text=_seed_lot_csv(["TLC-MIDIMPORT-A", "TLC-MIDIMPORT-B"]),
            )
        ),
        interrupt=lambda: controller.reset(_live_config()),
    )

    assert controller.store.all_between() == []
    assert response.accepted == 2
    assert response.stored == 0
    assert STALE_COMMIT_MESSAGE in (response.error or "")


def test_a_reset_landing_mid_demo_fixture_load_is_not_overwritten():
    response = _gated_run(
        seed=lambda: None,
        action=lambda: controller.load_demo_fixture(
            DemoFixtureId.LEAFY_GREENS_TRACE, DemoFixtureLoadRequest(reset=True)
        ),
        interrupt=lambda: controller.reset(_live_config()),
    )

    assert controller.store.all_between() == []
    assert response.loaded > 0
    assert response.stored == 0
    assert STALE_COMMIT_MESSAGE in (response.error or "")


def test_a_reset_landing_mid_retry_does_not_rewrite_the_cleared_store():
    """``update_many`` rewrites the entire log from ``_all_records()``, so a
    retry committing into a store that was cleared mid-flight is worse than
    a stray append -- it rewrites a freshly emptied file on behalf of rows
    that no longer exist. The epoch check drops the update instead.
    """
    response = _gated_run(
        seed=lambda: _seed_store(2, prefix="TLC-MIDRETRY", delivery_status="failed"),
        action=lambda: controller.retry_failed_delivery(DeliveryRetryRequest()),
        interrupt=lambda: controller.reset(_live_config()),
    )

    assert controller.store.all_between() == []
    assert response.posted == 2
    assert STALE_COMMIT_MESSAGE in (response.error or "")


def test_a_scenario_load_landing_mid_step_is_not_overwritten():
    """``load_scenario_save`` replaces the store's whole contents, so an
    in-flight step's records belong to the history it just replaced.
    """

    def seed() -> None:
        controller.store.add_many(
            [
                StoredEventRecord(
                    payload_source="lock-contention-suite",
                    event=_event("TLC-SAVED-1"),
                    destination_mode=DestinationMode.LIVE,
                    delivery_status="posted",
                )
            ]
        )

    async def interrupt() -> None:
        await controller.save_scenario(
            ScenarioId.LEAFY_GREENS_SUPPLIER, ScenarioSaveRequest()
        )
        await controller.load_scenario_save(ScenarioId.LEAFY_GREENS_SUPPLIER)

    response = _gated_run(
        seed=seed,
        action=lambda: controller.step(batch_size=2),
        interrupt=interrupt,
    )

    lot_codes = {record.event.traceability_lot_code for record in controller.store.all_between()}
    assert lot_codes == {"TLC-SAVED-1"}, lot_codes
    assert STALE_COMMIT_MESSAGE in (response.error or "")


def _start_then_stop(config: SimulationConfig):
    """Interrupt factory: run a full start(), then wind its loop back down.

    Both halves stay inside the gated scenario's own event loop so no task
    outlives the ``asyncio.run`` that created it.
    """

    async def interrupt() -> None:
        await controller.start(config)
        await controller.stop()

    return interrupt


def test_an_ordinary_start_on_the_same_persist_path_does_not_discard_the_batch(monkeypatch):
    """The negative control for the epoch bump.

    ``start()`` calls ``_store_configure`` on every press, almost always
    with the same ``persist_path``. Bumping the epoch there unconditionally
    would make a plain start silently throw away a concurrent import's
    records -- a far worse bug than the one being fixed. Only a genuine
    repoint invalidates.
    """
    monkeypatch.setattr(controller, "_run_loop", _idle_run_loop)

    response = _gated_run(
        seed=lambda: None,
        action=lambda: controller.step(batch_size=2),
        interrupt=_start_then_stop(_live_config()),
    )

    assert len(controller.store.all_between()) == 2
    assert response.error is None


def test_repointing_persist_path_mid_step_does_discard_the_batch(tmp_path, monkeypatch):
    """The other side of the same control: a ``start()`` that actually
    repoints the store at a different file reloads the in-memory ring from
    that file, so the in-flight batch's records belong to a store that is
    no longer the one on disk.
    """
    monkeypatch.setattr(controller, "_run_loop", _idle_run_loop)
    other_path = tmp_path / "other-events.jsonl"

    response = _gated_run(
        seed=lambda: None,
        action=lambda: controller.step(batch_size=2),
        interrupt=_start_then_stop(_live_config(persist_path=str(other_path))),
    )

    assert controller.store.all_between() == []
    assert STALE_COMMIT_MESSAGE in (response.error or "")


# ---------------------------------------------------------------------------
# The one mutual-exclusion guarantee releasing the lock would otherwise lose.
# ---------------------------------------------------------------------------


def test_two_concurrent_retries_do_not_both_post_the_same_failed_records():
    """``retry_failed_delivery`` selects the batch it posts from shared
    mutable state (the store's failed records), which every other delivery
    path does not. ``self._lock`` used to give it mutual exclusion for free
    by being held across the whole call; ``_retry_lock`` restores exactly
    that guarantee without putting the other paths back behind a delivery.

    Without ``_retry_lock`` both calls select the same two failed records
    and both POST them -- four events delivered for two records.
    """
    client = ProbingLiveClient(delay=0.02)

    async def scenario():
        controller.live_client = client
        first, second = await asyncio.gather(
            controller.retry_failed_delivery(DeliveryRetryRequest()),
            controller.retry_failed_delivery(DeliveryRetryRequest()),
        )
        return first, second

    asyncio.run(controller.reset(_live_config()))
    _seed_store(2, prefix="TLC-DOUBLE-RETRY", delivery_status="failed")
    original_client = controller.live_client
    try:
        first, second = asyncio.run(scenario())
    finally:
        controller.live_client = original_client

    assert sum(client.payload_sizes) == 2, (
        "the same failed records were delivered twice by overlapping retries: "
        f"{client.payload_sizes}"
    )
    statuses = sorted([first.status, second.status])
    assert statuses == ["empty", "posted"], statuses

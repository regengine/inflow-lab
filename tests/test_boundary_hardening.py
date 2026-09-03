"""The boundaries the previous commit claimed to harden, actually hardened.

An adversarial review of `141763c` found that its `csv_text` cap did not do
what its message said, and that two of its other changes traded one failure
for another. Each is pinned here.

* **The cap only rejected cleanly *above* itself.** `csv.field_size_limit()`
  is 131072 -- exactly 16x smaller than `MAX_CSV_TEXT_CHARS` -- and
  `csv.DictReader` raises `_csv.Error` on a wider field. That is a plain
  `Exception`, so the `ValueError` handler missed it and the request became a
  500. Everything from 128 KiB to 2 MiB still died in the handler; the test
  shipped with that commit passed only because it sat one character above the
  cap.

* **Rejecting cost more than accepting.** Pydantic puts the offending value in
  each error's `input` and FastAPI serialises that wholesale, so a 2 MiB
  rejection returned ~12.6 MB of escaped control characters -- a 6x amplifier
  bolted onto the surface the cap was meant to bound.

* **`friction[0]` broke non-sequences.** Indexing replaced iteration, so sets,
  dict views and generators raised `TypeError`; an *empty* generator went from
  a clean no-op to a crash.

* **`stop()` could orphan a run loop.** `await task` yields; a `start()`
  landing in that window installs a new task that the unconditional
  `self._task = None` then discarded, leaving a live loop writing records with
  `running` False and nothing able to stop it. Pre-existing, but that commit
  rewrote those lines without closing it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.mock_service import MAX_BATCH_EVENTS, MockRegEngineHTTPError, MockRegEngineService
from app.schemas.ingestion import MAX_CSV_TEXT_CHARS, IngestPayload

client = TestClient(app, raise_server_exceptions=False)

HEADER = (
    "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
    "location_name,timestamp\n"
)


def _import(csv_text: str):
    return client.post(
        "/api/import/csv", json={"import_type": "scheduled_events", "csv_text": csv_text}
    )


def test_an_oversized_field_does_not_500():
    # 131073 chars: one over csv's field limit, but 16x under the length cap.
    response = _import(HEADER + f"harvesting,TLC-1,{'x' * 131073},10,cases,Farm,2026-02-10T08:00:00Z\n")

    assert response.status_code == 200, response.status_code
    assert response.json()["errors"], "the failure was swallowed instead of reported"


def test_an_oversized_header_line_does_not_500():
    # Reading `.fieldnames` is what parses the header, so it raises before any
    # row is seen -- a distinct path from the one above.
    response = _import("x" * (MAX_CSV_TEXT_CHARS - 1))

    assert response.status_code == 200, response.status_code
    assert response.json()["errors"]


@pytest.mark.parametrize("size", [131073, MAX_CSV_TEXT_CHARS - 1])
def test_an_unparseable_document_is_never_reported_as_stored(size):
    body = _import("x" * size).json()

    assert body["stored"] == 0
    assert body["status"] != "accepted"


def test_a_rejected_document_is_not_echoed_back():
    # The reject path must not allocate more than the accept path would.
    oversized = "\x01" * (MAX_CSV_TEXT_CHARS + 1)
    response = _import(oversized)

    assert response.status_code == 422
    assert len(response.content) < 10_000, f"echoed {len(response.content)} bytes back"


def test_a_validation_error_still_says_which_field_was_wrong():
    # Trimming the echo must not cost the caller the information they need.
    response = client.post("/api/import/csv", json={"import_type": "nonsense", "csv_text": ""})

    assert response.status_code == 422
    assert "import_type" in response.text


# ---------------------------------------------------------------------------
# Friction codes accept any iterable again
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "friction",
    [("rate_limit",), ["rate_limit"], {"rate_limit"}, iter(["rate_limit"]), {"rate_limit": 1}.keys()],
)
def test_a_known_code_fires_for_any_iterable(friction):
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        MockRegEngineService().ingest(IngestPayload(source="t", events=[]), friction=friction)

    assert excinfo.value.status_code == 429


@pytest.mark.parametrize("empty", [(), [], set(), iter([])])
def test_an_empty_iterable_is_still_a_clean_no_op(empty):
    response = MockRegEngineService().ingest(IngestPayload(source="t", events=[]), friction=empty)

    assert response.accepted == 0


# ---------------------------------------------------------------------------
# The batch cap answers 422, like live RegEngine, not 500
# ---------------------------------------------------------------------------


def _event(index: int) -> dict:
    return {
        "cte_type": "harvesting",
        "traceability_lot_code": f"TLC-{index}",
        "product_description": "Romaine",
        "quantity": 10,
        "unit_of_measure": "cases",
        "location_name": "Valley Fresh Farms",
        "timestamp": "2026-02-10T08:00:00Z",
        "kdes": {"harvest_date": "2026-02-10", "reference_document": "REF-1"},
    }


def test_an_oversized_batch_is_422_not_500():
    response = client.post(
        "/api/mock/regengine/ingest",
        json={"source": "cap-test", "events": [_event(i) for i in range(MAX_BATCH_EVENTS + 1)]},
    )

    assert response.status_code == 422, response.status_code


def test_a_batch_at_the_cap_is_still_accepted():
    response = client.post(
        "/api/mock/regengine/ingest",
        json={"source": "cap-test", "events": [_event(i) for i in range(MAX_BATCH_EVENTS)]},
    )

    assert response.status_code == 200, response.status_code


# ---------------------------------------------------------------------------
# stop() must not discard a task it did not start
# ---------------------------------------------------------------------------


def test_stop_does_not_orphan_a_concurrently_started_run_loop(tmp_path):
    from app.controller import SimulationController
    from app.engine import LegitFlowEngine
    from app.mock_service import MockRegEngineService as Mock
    from app.regengine_client import LiveRegEngineClient
    from app.scenario_saves import ScenarioSaveStore
    from app.schemas.domain import DestinationMode
    from app.schemas.simulation import DeliveryConfig, SimulationConfig
    from app.store import EventStore

    controller = SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / "events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=Mock(),
        live_client=LiveRegEngineClient(),
        tenant_id="orphan-test",
    )
    controller.config = SimulationConfig(
        persist_path=str(tmp_path / "events.jsonl"),
        batch_size=1,
        interval_seconds=0.01,
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )

    async def scenario() -> tuple[bool, bool]:
        await controller.start(controller.config)
        await asyncio.sleep(0.05)
        # Race a start() into stop()'s `await task` window.
        stopping = asyncio.create_task(controller.stop())
        await asyncio.sleep(0)
        await controller.start(controller.config)
        await stopping
        running = controller.running
        installed = controller._task is not None
        await controller.stop()
        return running, installed

    running, installed = asyncio.run(scenario())

    # Either the restart won (running, and reachable) or the stop won (fully
    # stopped). What must never happen is a live loop with `_task` cleared.
    assert running == installed, "a run loop was left running with no task installed"

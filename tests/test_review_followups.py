"""Follow-up findings from #210 and #211 that are resident on `main`.

Both issues collect review findings against the unmerged audit stack, so most
of their contents describe code that does not exist here yet. These four do
exist on `main`, and each is pinned below:

* **A run-loop crash was silent** (#211). `_run_loop` had no `try/except`, so
  a store failure mid-run ended the task with `self._task` still installed.
  `running` read False (`task.done()` was already true), `stop()` therefore
  early-returned, and the SSE revision never moved -- so the console went on
  showing a run that had died, and asyncio logged the exception as never
  retrieved at GC time. (The "never retrieved" half is not pinned here: it
  fires when the task is collected, after the loop the test ran on is closed,
  so an in-process assertion on it cannot tell the two versions apart.)

* **`csv_text` had no length bound** (#210, and #136's second acceptance
  criterion). The importer has no row cap of its own and parses the whole
  document into memory.

* **`not_configured` restated the rule instead of naming the missing field**
  (#210).

* **An unknown mock friction code was skipped in silence** (#210). The
  `MockFrictionCode` literal already pins every operator-reachable path, so
  what is left is a direct caller of the service layer -- the same shape as
  the `store.py` residue #211 lists beside it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.controller import SimulationController
from app.engine import LegitFlowEngine
from app.main import app
from app.mock_service import FRICTION_RESPONSES, MockRegEngineHTTPError, MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.schemas.domain import DestinationMode
from app.schemas.ingestion import MAX_CSV_TEXT_CHARS, IngestPayload
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import EventStore

client = TestClient(app)


def _controller(tmp_path: Path) -> SimulationController:
    controller = SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / "events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
        tenant_id="followups-test",
    )
    controller.config = SimulationConfig(
        persist_path=str(tmp_path / "events.jsonl"),
        batch_size=2,
        interval_seconds=0.01,
        delivery=DeliveryConfig(mode=DestinationMode.MOCK),
    )
    return controller


def _break_store(controller: SimulationController) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("disk gone")

    controller.store.add_many = boom  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# #211 -- a run-loop crash must be visible, and must not wedge the controller
# ---------------------------------------------------------------------------


def test_a_crashed_run_loop_bumps_the_revision(tmp_path):
    # The decisive one. The console learns a run died only through the SSE
    # revision, so a crash that does not move it is invisible.
    controller = _controller(tmp_path)

    async def scenario() -> tuple[int, int]:
        await controller.start(controller.config)
        await asyncio.sleep(0.05)
        before = controller.revision
        _break_store(controller)
        await asyncio.sleep(0.15)
        return before, controller.revision

    before, after = asyncio.run(scenario())

    assert after > before, "a crashed run loop left the SSE revision untouched"


def test_a_crashed_run_loop_is_logged_with_its_traceback(tmp_path, caplog):
    controller = _controller(tmp_path)

    async def scenario() -> None:
        await controller.start(controller.config)
        await asyncio.sleep(0.05)
        _break_store(controller)
        await asyncio.sleep(0.15)

    with caplog.at_level(logging.ERROR, logger="app.controller"):
        asyncio.run(scenario())

    failures = [r for r in caplog.records if "run loop stopped on an unhandled error" in r.message]
    assert len(failures) == 1
    assert failures[0].exc_info is not None, "logged without the traceback"


def test_stop_clears_a_crashed_task(tmp_path):
    # `stop()` gated on `self.running`, which a finished task makes False, so
    # the dead task stayed installed and the controller could not restart.
    controller = _controller(tmp_path)

    async def scenario() -> bool:
        await controller.start(controller.config)
        await asyncio.sleep(0.05)
        _break_store(controller)
        await asyncio.sleep(0.15)
        await controller.stop()
        return controller._task is None

    assert asyncio.run(scenario()), "stop() left the crashed task installed"


def test_a_clean_stop_still_works(tmp_path):
    # The guard must not have broken the ordinary path.
    controller = _controller(tmp_path)

    async def scenario() -> tuple[bool, bool]:
        await controller.start(controller.config)
        await asyncio.sleep(0.05)
        was_running = controller.running
        await controller.stop()
        return was_running, controller.running

    was_running, still_running = asyncio.run(scenario())

    assert was_running and not still_running
    assert controller._task is None


def test_stopping_a_controller_that_never_started_is_a_no_op(tmp_path):
    controller = _controller(tmp_path)

    async def scenario() -> int:
        before = controller.revision
        await controller.stop()
        return controller.revision - before

    assert asyncio.run(scenario()) == 0


# ---------------------------------------------------------------------------
# #210 -- csv_text is bounded
# ---------------------------------------------------------------------------


def test_an_oversized_csv_import_is_rejected_as_422():
    response = client.post(
        "/api/import/csv",
        json={"import_type": "seed_lots", "csv_text": "x" * (MAX_CSV_TEXT_CHARS + 1)},
    )

    assert response.status_code == 422, response.status_code


def test_a_normal_csv_import_is_unaffected():
    csv_text = (
        "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
        "TLC-CAP-1,Romaine,10,cases,Valley Fresh Farms\n"
    )

    response = client.post(
        "/api/import/csv", json={"import_type": "seed_lots", "csv_text": csv_text}
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# #210 -- not_configured names the field that is missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "named", "not_named"),
    [
        ({"tenant_id": "acme"}, "an API key", "a tenant id"),
        ({"api_key": "sk-test"}, "a tenant id", "an API key"),
    ],
)
def test_not_configured_names_only_the_missing_field(body, named, not_named):
    response = client.post("/api/integration/test", json=body)

    assert response.status_code == 200, response.text
    detail = response.json()["detail"]
    assert named in detail
    assert f"Missing {not_named}" not in detail


def test_not_configured_names_both_when_both_are_missing():
    # An empty body stays in mock mode and short-circuits before the probe, so
    # reach the branch with an endpoint and no credentials.
    detail = client.post(
        "/api/integration/test", json={"endpoint": "https://example.test/api/v1/webhooks/ingest"}
    ).json()["detail"]

    assert "an API key" in detail and "a tenant id" in detail


# ---------------------------------------------------------------------------
# #210 -- an unknown friction code is rejected, not skipped
# ---------------------------------------------------------------------------


def _payload() -> IngestPayload:
    return IngestPayload(source="friction-test", events=[])


def test_an_unknown_friction_code_is_rejected():
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        MockRegEngineService().ingest(_payload(), friction=("rate_limt",))

    assert excinfo.value.status_code == 422
    assert "rate_limt" in excinfo.value.detail


def test_an_unknown_code_is_reported_regardless_of_position():
    # Checked up front, so a known code listed first does not mask the typo.
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        MockRegEngineService().ingest(_payload(), friction=("rate_limit", "rate_limt"))

    assert excinfo.value.status_code == 422


@pytest.mark.parametrize("code", sorted(FRICTION_RESPONSES))
def test_every_known_friction_code_still_fires(code):
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        MockRegEngineService().ingest(_payload(), friction=(code,))

    assert excinfo.value.status_code == FRICTION_RESPONSES[code][0]


def test_no_friction_is_still_the_default():
    response = MockRegEngineService().ingest(_payload())

    assert response.accepted == 0

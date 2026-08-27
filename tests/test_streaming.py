"""The SSE snapshot stream and the controller revision signal it is built
on (split out of tests/test_api.py for #132).
"""

import asyncio
import json

from app.main import controller
from tests.support.api_client import client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_sse_stream_emits_initial_snapshot():
    response = client.get("/api/simulate/stream?limit=5&once=true")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.splitlines() if line]

    assert lines[0] == "event: snapshot"
    data_line = next(line for line in lines if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["revision"] == controller.revision
    assert payload["status"]["running"] is False
    assert payload["events"] == []


def test_controller_revision_notifies_after_step():
    async def wait_for_step_update() -> dict:
        starting_revision = controller.revision
        waiter = asyncio.create_task(controller.wait_for_revision(starting_revision, timeout=1.0))
        await controller.step(batch_size=1)
        observed_revision = await waiter
        return controller.snapshot(event_limit=5) | {"observed_revision": observed_revision}

    snapshot = asyncio.run(wait_for_step_update())

    assert snapshot["observed_revision"] == snapshot["revision"]
    assert snapshot["status"]["stats"]["total_records"] == 1
    assert len(snapshot["events"]) == 1

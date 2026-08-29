"""The simulation control surface -- /api/simulate/{start,stop,reset,replay}
-- covering persist_path handling, scenario switching and replay
semantics (split out of tests/test_api.py for #132).
"""

import asyncio

from app.main import controller
from app.scenarios import ScenarioId, get_scenario
from app.schemas.simulation import SimulationConfig
from tests.support.api_client import client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_start_applies_configured_persist_path_and_keeps_mock_default(tmp_path):
    custom_path = tmp_path / "start-events.jsonl"
    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
            }
        },
    )
    try:
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["persist_path"] == str(custom_path)
        assert body["config"]["delivery"]["mode"] == "mock"
        assert body["stats"]["persist_path"] == str(custom_path)
    finally:
        client.post("/api/simulate/stop")


def test_stop_interrupts_long_interval_sleep(tmp_path):
    async def start_and_stop() -> bool:
        await controller.start(
            SimulationConfig(
                interval_seconds=60,
                batch_size=1,
                seed=204,
                persist_path=str(tmp_path / "long-interval-events.jsonl"),
            )
        )
        await asyncio.wait_for(controller.stop(), timeout=1.0)
        return controller.running

    assert asyncio.run(start_and_stop()) is False


def test_reset_applies_configured_persist_path_for_next_step(tmp_path):
    custom_path = tmp_path / "reset-events.jsonl"
    response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["persist_path"] == str(custom_path)
    assert status["config"]["delivery"]["mode"] == "mock"
    assert status["stats"]["persist_path"] == str(custom_path)

    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200
    assert step_response.json()["generated"] == 1
    assert custom_path.exists()
    assert len(custom_path.read_text(encoding="utf-8").splitlines()) == 1


def test_replay_current_persisted_log_posts_without_rewriting_records(tmp_path):
    custom_path = tmp_path / "replay-events.jsonl"
    reset_response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 2,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert reset_response.status_code == 200
    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200

    original_log = custom_path.read_text(encoding="utf-8")
    original_events = client.get("/api/events?limit=10").json()["events"]

    response = client.post("/api/simulate/replay")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["read"] == 2
    assert body["replayed"] == 2
    assert body["posted"] == 2
    assert body["failed"] == 0
    assert body["delivery_mode"] == "mock"
    assert body["persist_path"] == str(custom_path)
    assert body["response"]["total"] == 2

    assert custom_path.read_text(encoding="utf-8") == original_log
    assert client.get("/api/events?limit=10").json()["events"] == original_events


def test_replay_accepts_override_path_and_delivery_none(tmp_path):
    current_path = tmp_path / "current-events.jsonl"
    override_path = tmp_path / "override-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(current_path),
        },
    )
    client.post("/api/simulate/step")
    override_path.write_text(current_path.read_text(encoding="utf-8"), encoding="utf-8")
    original_override_log = override_path.read_text(encoding="utf-8")

    response = client.post(
        "/api/simulate/replay",
        json={
            "persist_path": str(override_path),
            "source": "replay-suite",
            "delivery": {"mode": "none"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rebuilt"
    assert body["read"] == 1
    assert body["replayed"] == 1
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["source"] == "replay-suite"
    assert body["delivery_mode"] == "none"
    assert body["persist_path"] == str(override_path)
    assert body["response"] is None
    assert override_path.read_text(encoding="utf-8") == original_override_log


def test_replay_missing_log_returns_empty_counts(tmp_path):
    missing_path = tmp_path / "missing-events.jsonl"

    response = client.post("/api/simulate/replay", json={"persist_path": str(missing_path)})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["read"] == 0
    assert body["replayed"] == 0
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["persist_path"] == str(missing_path)


def test_reset_applies_scenario_config_and_keeps_mock_delivery_default(tmp_path):
    custom_path = tmp_path / "retailer-events.jsonl"
    response = client.post(
        "/api/simulate/reset",
        json={
            "scenario": "retailer_readiness_demo",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == "retailer_readiness_demo"
    assert status["config"]["delivery"]["mode"] == "mock"
    assert status["stats"]["engine"]["scenario"] == "retailer_readiness_demo"

    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200
    events = client.get("/api/events?limit=1").json()["events"]
    expected_products = {product.name for product in get_scenario(ScenarioId.RETAILER_READINESS_DEMO).products}

    assert events[0]["event"]["product_description"] in expected_products


def test_start_applies_scenario_change_even_with_existing_records(tmp_path):
    custom_path = tmp_path / "scenario-switch-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "leafy_greens_supplier",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    client.post("/api/simulate/step")

    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "scenario": "fresh_cut_processor",
                "interval_seconds": 10,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
            }
        },
    )
    try:
        assert response.status_code == 200
        status = response.json()
        assert status["config"]["scenario"] == "fresh_cut_processor"
        assert status["stats"]["engine"]["scenario"] == "fresh_cut_processor"
    finally:
        client.post("/api/simulate/stop")

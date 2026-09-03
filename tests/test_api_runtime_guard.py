"""Runtime invariants the controller enforces around the event loop and process model.

Covers two audit items:

* #161 -- simulation run/stop state is per-process, so multi-worker startup is
  refused rather than silently booting into a state where Stop does nothing.
* #136 -- the event store's blocking file I/O and the CPU-bound CSV parser run
  on worker threads, not on the single event loop.
"""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from app.controller import (
    MultiProcessRuntimeError,
    SimulationController,
    enforce_single_process_runtime,
)
from app.main import app, controller
from app.schemas.simulation import SimulationConfig


client = TestClient(app)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


# --- #161: single-process is enforced, not assumed -------------------------


@pytest.mark.parametrize(
    "variable",
    [
        "WEB_CONCURRENCY",
        "UVICORN_WORKERS",
        "GUNICORN_WORKERS",
        "RAILWAY_REPLICA_COUNT",
        "WEB_REPLICAS",
    ],
)
def test_multi_worker_configuration_is_refused(variable):
    with pytest.raises(MultiProcessRuntimeError) as excinfo:
        enforce_single_process_runtime({variable: "4"})

    message = str(excinfo.value)
    assert variable in message
    assert "DEPLOYMENT_PROFILES.md" in message


@pytest.mark.parametrize("value", ["", "   ", "1", " 1 ", "not-a-number", "0"])
def test_single_or_unspecified_worker_count_is_allowed(value):
    # Unset, explicitly single, and malformed values are all the documented
    # single-process default; only an explicit count above 1 is a problem.
    enforce_single_process_runtime({"WEB_CONCURRENCY": value})


def test_no_worker_variables_at_all_is_allowed():
    enforce_single_process_runtime({})


def test_controller_refuses_to_start_in_a_multi_worker_process(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")

    with pytest.raises(MultiProcessRuntimeError):
        SimulationController(
            engine=controller.engine,
            store=controller.store,
            scenario_saves=controller.scenario_saves,
            mock_service=controller.mock_service,
            live_client=controller.live_client,
        )


# --- #136: store I/O and CSV parsing stay off the event loop ---------------


def _record_calling_thread(monkeypatch, owner, attribute):
    """Wrap `owner.attribute`, recording the thread each call runs on."""
    original = getattr(owner, attribute)
    threads: list[threading.Thread] = []

    def wrapper(*args, **kwargs):
        threads.append(threading.current_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, attribute, wrapper)
    return threads


def test_step_persists_records_off_the_event_loop_thread(monkeypatch):
    threads = _record_calling_thread(monkeypatch, controller.store, "add_many")

    response = client.post("/api/simulate/step")

    assert response.status_code == 200
    assert threads, "add_many was never called"
    assert all(thread is not threading.main_thread() for thread in threads)


def test_csv_import_parses_and_persists_off_the_event_loop_thread(monkeypatch):
    import app.controller as controller_module

    parse_threads = _record_calling_thread(monkeypatch, controller_module, "parse_csv_import")
    store_threads = _record_calling_thread(monkeypatch, controller.store, "add_many")

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "seed_lots",
            "csv_text": (
                "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
                "TLC-OFFLOOP,Romaine Hearts,12,cases,Valley Fresh Farms\n"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["stored"] == 1
    assert parse_threads and all(t is not threading.main_thread() for t in parse_threads)
    assert store_threads and all(t is not threading.main_thread() for t in store_threads)


def test_delivery_retry_rewrites_the_log_off_the_event_loop_thread(monkeypatch):
    from app.regengine_client import LiveRegEngineDeliveryError

    class FailingLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            raise LiveRegEngineDeliveryError("outage", {"delivery_mode": "live"})

    original_live_client = controller.live_client
    controller.live_client = FailingLiveClient()
    try:
        client.post(
            "/api/simulate/reset",
            json={
                "batch_size": 1,
                "seed": 204,
                "delivery": {
                    "mode": "live",
                    "api_key": "live-api-secret",
                    "tenant_id": "live-tenant-secret",
                },
            },
        )
        assert client.post("/api/simulate/step").json()["delivery_status"] == "failed"
    finally:
        controller.live_client = original_live_client

    threads = _record_calling_thread(monkeypatch, controller.store, "update_many")

    retry = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}})

    assert retry.status_code == 200
    assert retry.json()["status"] == "posted"
    assert threads and all(thread is not threading.main_thread() for thread in threads)


def test_scenario_load_replaces_the_log_off_the_event_loop_thread(monkeypatch):
    client.post("/api/simulate/step")
    save = client.post("/api/scenario-saves/leafy_greens_supplier", json={})
    assert save.status_code == 200

    threads = _record_calling_thread(monkeypatch, controller.store, "replace_all")

    load = client.post("/api/scenario-saves/leafy_greens_supplier/load")

    assert load.status_code == 200
    assert threads and all(thread is not threading.main_thread() for thread in threads)

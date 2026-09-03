"""Saving and reloading a scenario's config plus event log through the
/api/scenario-saves routes (split out of tests/test_api.py for #132).
"""

from app.main import scenario_saves
from tests.support.api_client import client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_scenario_save_load_restores_config_and_event_log(tmp_path):
    scenario_saves.configure(str(tmp_path / "scenario-saves"))
    fresh_path = tmp_path / "fresh-cut-events.jsonl"
    retailer_path = tmp_path / "retailer-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "fresh_cut_processor",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(fresh_path),
            "delivery": {"mode": "none"},
        },
    )
    client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={
            "source": "scenario-save-suite",
            "delivery": {"mode": "none"},
        },
    )

    save_response = client.post("/api/scenario-saves/fresh_cut_processor")
    assert save_response.status_code == 200
    save_body = save_response.json()
    assert save_body["status"] == "saved"
    assert save_body["save"]["scenario"] == "fresh_cut_processor"
    assert save_body["save"]["record_count"] == 13
    assert save_body["config"]["source"] == "scenario-save-suite"
    assert save_body["config"]["delivery"]["mode"] == "none"

    list_response = client.get("/api/scenario-saves")
    assert list_response.status_code == 200
    assert [save["scenario"] for save in list_response.json()["saves"]] == ["fresh_cut_processor"]

    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "retailer_readiness_demo",
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(retailer_path),
            "delivery": {"mode": "none"},
        },
    )
    client.post("/api/simulate/step")
    assert client.get("/api/simulate/status").json()["stats"]["total_records"] == 1

    load_response = client.post("/api/scenario-saves/fresh_cut_processor/load")
    assert load_response.status_code == 200
    load_body = load_response.json()
    assert load_body["status"] == "loaded"
    assert load_body["loaded_records"] == 13
    assert load_body["config"]["scenario"] == "fresh_cut_processor"
    assert load_body["config"]["persist_path"] == str(fresh_path)

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == "fresh_cut_processor"
    assert status["stats"]["total_records"] == 13
    assert status["stats"]["engine"]["scenario"] == "fresh_cut_processor"
    lineage = client.get("/api/lineage/TLC-DEMO-FC-OUT-001").json()
    assert len(lineage["records"]) == 13


def test_scenario_save_sanitizes_live_credentials_and_missing_load_returns_404(tmp_path):
    scenario_saves.configure(str(tmp_path / "scenario-saves"))
    response = client.post(
        "/api/scenario-saves/retailer_readiness_demo",
        json={
            "config": {
                "source": "live-suite",
                "scenario": "retailer_readiness_demo",
                "interval_seconds": 2,
                "batch_size": 2,
                "seed": 204,
                "persist_path": str(tmp_path / "live-events.jsonl"),
                "delivery": {
                    "mode": "live",
                    "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
                    "api_key": "secret-key",
                    "tenant_id": "tenant-123",
                },
            }
        },
    )

    assert response.status_code == 200
    delivery = response.json()["config"]["delivery"]
    assert delivery["mode"] == "mock"
    assert delivery["endpoint"] is None
    assert delivery["api_key"] is None
    assert delivery["tenant_id"] is None

    missing_response = client.post("/api/scenario-saves/leafy_greens_supplier/load")
    assert missing_response.status_code == 404

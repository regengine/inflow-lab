"""Core simulation API surface: stepping, the event log, the scenario
catalog and the readiness-audit summary on /api/simulate/status.

#132 split this module's other subsystems into focused siblings --
tests/test_auth_cors.py, tests/test_operator_tenants.py,
tests/test_scenario_saves.py, tests/test_demo_fixtures.py,
tests/test_exports.py, tests/test_live_delivery.py,
tests/test_csv_import.py, tests/test_streaming.py,
tests/test_mock_ingest.py and tests/test_simulation_lifecycle.py -- which
share this file's TestClient and per-test reset through
tests/support/api_client.py.
"""

from tests.support.api_client import client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_single_step_generates_mock_events():
    response = client.post("/api/simulate/step")
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated"] == 3
    assert len(payload["lot_codes"]) == 3
    assert payload["posted"] == 3
    assert payload["failed"] == 0

    events_response = client.get("/api/events?limit=10")
    assert events_response.status_code == 200
    events = events_response.json()["events"]
    assert len(events) == 3
    first_event = events[0]["event"]
    assert "cte_type" in first_event
    assert "traceability_lot_code" in first_event
    assert "kdes" in first_event


def test_scenario_catalog_endpoint_lists_supported_presets():
    response = client.get("/api/scenarios")

    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert [scenario["id"] for scenario in scenarios] == [
        "leafy_greens_supplier",
        "fresh_cut_processor",
        "retailer_readiness_demo",
        "seafood_first_receiver",
        "dairy_continuous_flow",
        "copacker_nut_butter",
        "broadline_distributor",
        "foodservice_restaurant_group",
        "shell_egg_producer",
    ]
    assert all(scenario["label"] for scenario in scenarios)
    assert {scenario["industry_type"] for scenario in scenarios} >= {"produce", "seafood", "dairy"}
    assert {scenario["operation_type"] for scenario in scenarios} >= {"supplier", "processor", "retailer", "first_receiver"}


def test_status_includes_backend_audit_summary():
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "leafy_greens_supplier",
            "batch_size": 1,
            "seed": 204,
        },
    )
    client.post("/api/simulate/step")

    status = client.get("/api/simulate/status").json()
    audit = status["stats"]["audit"]

    assert audit["industry_type"] == "produce"
    assert audit["reference_format"] == "GS1"
    assert isinstance(audit["score"], int)
    assert audit["total"] >= 1
    assert isinstance(audit["checks"], list)


def test_status_audit_tracks_seafood_readiness_shape():
    client.post(
        "/api/simulate/reset",
        json={
            "scenario": "seafood_first_receiver",
            "batch_size": 1,
            "seed": 204,
        },
    )
    client.post("/api/simulate/step")

    status = client.get("/api/simulate/status").json()
    audit = status["stats"]["audit"]

    assert audit["industry_type"] == "seafood"
    assert audit["requires_cooling"] is False
    assert any(check["label"] == "Vessel-linked receiving" for check in audit["checks"])

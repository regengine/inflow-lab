"""Unknown and misplaced request-body fields must be rejected, not dropped.

Regression cover for #143. The bug it describes: `POST /api/simulate/reset`
took a bare `SimulationConfig` at the top level while `/api/simulate/start`
took it wrapped under `config`, and no schema set `extra="forbid"` -- so
posting `/start`'s shape to `/reset` returned 200 having silently discarded
the whole override and reset to hard-coded defaults.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _status_scenario() -> str:
    return client.get("/api/simulate/status").json()["config"]["scenario"]


def test_reset_accepts_start_wrapped_shape_and_applies_it():
    # The exact body from #143: /start's shape posted to /reset. It used to
    # return 200 and fall back to the SimulationConfig default.
    response = client.post(
        "/api/simulate/reset",
        json={"config": {"scenario": "dairy_continuous_flow", "batch_size": 1, "seed": 204}},
    )

    assert response.status_code == 200, response.text
    assert _status_scenario() == "dairy_continuous_flow"


def test_reset_still_accepts_the_legacy_bare_config_shape():
    response = client.post(
        "/api/simulate/reset",
        json={"scenario": "leafy_greens_supplier", "batch_size": 1, "seed": 204},
    )

    assert response.status_code == 200, response.text
    assert _status_scenario() == "leafy_greens_supplier"


def test_start_and_reset_accept_an_identical_body():
    body = {"config": {"scenario": "dairy_continuous_flow", "batch_size": 1, "seed": 204}}

    assert client.post("/api/simulate/start", json=body).status_code == 200
    client.post("/api/simulate/stop")
    assert client.post("/api/simulate/reset", json=body).status_code == 200
    assert _status_scenario() == "dairy_continuous_flow"

    client.post("/api/simulate/reset", json={"scenario": "leafy_greens_supplier", "seed": 204})


def test_unknown_field_in_config_is_rejected():
    response = client.post(
        "/api/simulate/reset",
        json={"config": {"scenario": "leafy_greens_supplier", "scenarioo": "typo"}},
    )

    assert response.status_code == 422
    assert "scenarioo" in response.text


def test_unknown_field_alongside_config_is_rejected():
    response = client.post(
        "/api/simulate/reset",
        json={"config": {"scenario": "leafy_greens_supplier"}, "batch_size": 3},
    )

    assert response.status_code == 422


def test_unknown_field_in_start_body_is_rejected():
    response = client.post(
        "/api/simulate/start",
        json={"config": {"scenario": "leafy_greens_supplier"}, "autostart": True},
    )

    assert response.status_code == 422


def test_unknown_field_in_delivery_override_is_rejected():
    response = client.post(
        "/api/simulate/reset",
        json={"config": {"delivery": {"mode": "mock", "endpont": "https://example.test/"}}},
    )

    assert response.status_code == 422
    assert "endpont" in response.text


def test_unknown_field_in_integration_configure_is_rejected():
    response = client.post("/api/integration/configure", json={"endpont": "https://example.test/"})

    assert response.status_code == 422
    assert "endpont" in response.text


def test_unknown_field_in_connection_test_is_rejected():
    response = client.post("/api/integration/test", json={"api_ky": "secret"})

    assert response.status_code == 422


def test_unknown_field_in_csv_import_is_rejected():
    response = client.post(
        "/api/import/csv",
        json={"import_type": "cte_events", "csv_text": "", "sorce": "typo"},
    )

    assert response.status_code == 422


def test_unknown_field_in_delivery_retry_is_rejected():
    response = client.post("/api/delivery/retry", json={"limitt": 5})

    assert response.status_code == 422


def test_unknown_field_in_replay_is_rejected():
    response = client.post("/api/simulate/replay", json={"persist_pth": "data/events.jsonl"})

    assert response.status_code == 422


def test_mock_ingest_still_ignores_unknown_event_fields():
    # Deliberate asymmetry, not an oversight: RegEngine's own IngestEvent sets
    # no model_config and therefore ignores unknown keys. A stricter mock would
    # reject batches live ingest accepts, inverting the parity this simulator
    # exists to demonstrate.
    response = client.post(
        "/api/mock/regengine/ingest",
        json={
            "source": "parity-test",
            "events": [
                {
                    "cte_type": "harvesting",
                    "traceability_lot_code": "TLC-PARITY-1",
                    "product_description": "Romaine Lettuce",
                    "quantity": 10,
                    "unit_of_measure": "cases",
                    "location_name": "Valley Fresh Farms",
                    "timestamp": "2026-02-10T08:00:00Z",
                    "kdes": {"harvest_date": "2026-02-10"},
                    "some_field_regengine_would_ignore": "ok",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text

"""Demo fixture catalog and playback.

Moved verbatim out of ``test_api.py`` (see #132).
"""

import asyncio
import base64
import json

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.simulation import SimulationConfig


client = TestClient(app)

SECRET = "regengine-live-secret"
PUBLIC_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def assert_json_omits(payload: object, *needles: str) -> None:
    dumped = json.dumps(payload, sort_keys=True)
    for needle in needles:
        assert needle not in dumped


def setup_function() -> None:
    # reset shared app state between tests

    asyncio.run(controller.reset(SimulationConfig()))


def test_demo_fixture_catalog_lists_supported_playbacks():
    response = client.get("/api/demo-fixtures")

    assert response.status_code == 200
    fixtures = response.json()["fixtures"]
    assert [fixture["id"] for fixture in fixtures] == [
        "leafy_greens_trace",
        "fresh_cut_transformation",
        "retailer_handoff",
    ]
    assert fixtures[1]["scenario"] == "fresh_cut_processor"
    assert fixtures[1]["event_count"] == 13
    assert "TLC-DEMO-FC-OUT-001" in fixtures[1]["lot_codes"]


def test_load_demo_fixture_resets_store_and_preserves_transformation_lineage(tmp_path):
    custom_path = tmp_path / "demo-fixture-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    client.post("/api/simulate/step")

    response = client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        json={
            "reset": True,
            "source": "fixture-suite",
            "delivery": {"mode": "none"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "loaded"
    assert body["fixture_id"] == "fresh_cut_transformation"
    assert body["scenario"] == "fresh_cut_processor"
    assert body["loaded"] == 13
    assert body["stored"] == 13
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["source"] == "fixture-suite"
    assert body["delivery_mode"] == "none"
    assert body["lot_codes"] == [
        "TLC-DEMO-FC-HARVEST-001",
        "TLC-DEMO-FC-HARVEST-002",
        "TLC-DEMO-FC-PACK-001",
        "TLC-DEMO-FC-PACK-002",
        "TLC-DEMO-FC-OUT-001",
    ]

    status = client.get("/api/simulate/status").json()
    assert status["config"]["scenario"] == "fresh_cut_processor"
    assert status["stats"]["total_records"] == 13
    assert status["stats"]["by_delivery_status"] == {"generated": 13}

    lineage = client.get("/api/lineage/TLC-DEMO-FC-OUT-001").json()
    lineage_lot_codes = {record["event"]["traceability_lot_code"] for record in lineage["records"]}
    assert {
        "TLC-DEMO-FC-HARVEST-001",
        "TLC-DEMO-FC-HARVEST-002",
        "TLC-DEMO-FC-PACK-001",
        "TLC-DEMO-FC-PACK-002",
        "TLC-DEMO-FC-OUT-001",
    } <= lineage_lot_codes
    assert {
        (edge["source_lot_code"], edge["target_lot_code"])
        for edge in lineage["edges"]
    } >= {
        ("TLC-DEMO-FC-PACK-001", "TLC-DEMO-FC-OUT-001"),
        ("TLC-DEMO-FC-PACK-002", "TLC-DEMO-FC-OUT-001"),
    }

    export_response = client.get(
        "/api/mock/regengine/export/fda-request?preset=lot_trace&traceability_lot_code=TLC-DEMO-FC-OUT-001"
    )
    assert export_response.status_code == 200
    assert "BATCH-DEMO-FC-001" in export_response.text


def test_load_demo_fixture_posts_to_mock_when_requested(tmp_path):
    custom_path = tmp_path / "demo-fixture-mock-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )

    response = client.post(
        "/api/demo-fixtures/retailer_handoff/load",
        json={
            "delivery": {"mode": "mock"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "loaded"
    assert body["loaded"] == 7
    assert body["posted"] == 7
    assert body["delivery_mode"] == "mock"
    assert body["delivery_attempts"] == 1
    assert body["response"]["total"] == 7

    record = client.get("/api/events?limit=1").json()["events"][0]
    assert record["delivery_status"] == "posted"
    assert record["delivery_attempts"] == 1
    assert record["delivery_response"]["sha256_hash"]

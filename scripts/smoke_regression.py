from __future__ import annotations

import base64
import os
import shutil
import sys
from functools import partial
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.main import app

# The app resolves its data root from REGENGINE_DATA_DIR at import time
# (app.tenancy.DATA_ROOT), so this script must ask tenancy where a tenant
# lives rather than assume the default ./data. Hardcoding it made the
# persist_path assertion below fail on a literal string mismatch, and made
# the cleanup rmtree miss -- leaving release-smoke tenants behind in
# whatever store the operator's shell actually pointed at (#108).
from app.tenancy import tenant_dir, tenant_events_path
from scripts import _smoke_common


TENANTS = ["release-smoke-main", "release-smoke-other"]


class SmokeFailure(AssertionError):
    pass


def main() -> int:
    client = TestClient(app)
    try:
        run_smoke(client)
    finally:
        cleanup_smoke_tenants()
    print("Release smoke regression passed.")
    return 0


def run_smoke(client: TestClient) -> None:
    main_headers = request_headers(TENANTS[0])
    other_headers = request_headers(TENANTS[1])

    health = assert_json(client.get("/api/health", headers=main_headers), 200)
    assert_equal(health["tenant"], TENANTS[0], "health tenant")
    assert_equal(
        health["auth"]["enabled"],
        bool(os.getenv("REGENGINE_BASIC_AUTH_USERNAME") and os.getenv("REGENGINE_BASIC_AUTH_PASSWORD")),
        "health auth enabled",
    )
    assert_equal(health["auth"]["uses_default_storage"], False, "health tenant storage scope")
    reset_response = client.post(
        "/api/simulate/reset",
        headers=main_headers,
        json={
            "scenario": "fresh_cut_processor",
            "batch_size": 1,
            "seed": 204,
            "delivery": {"mode": "none"},
        },
    )
    assert_status(reset_response, 200)

    fixture_response = client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        headers=main_headers,
        json={
            "source": "release-smoke",
            "delivery": {"mode": "none"},
        },
    )
    fixture = assert_json(fixture_response, 200)
    assert_equal(fixture["stored"], 13, "fixture stored events")
    assert_equal(fixture["delivery_mode"], "none", "fixture delivery mode")

    status = assert_json(client.get("/api/simulate/status", headers=main_headers), 200)
    assert_equal(status["stats"]["total_records"], 13, "fixture status total")
    assert_equal(
        status["config"]["persist_path"],
        str(tenant_events_path(TENANTS[0])),
        "tenant persist path",
    )

    lineage = assert_json(
        client.get("/api/lineage/TLC-DEMO-FC-OUT-001", headers=main_headers),
        200,
    )
    lineage_lots = {record["event"]["traceability_lot_code"] for record in lineage["records"]}
    assert_in("TLC-DEMO-FC-HARVEST-001", lineage_lots, "lineage upstream harvest lot")
    assert_in("TLC-DEMO-FC-PACK-001", lineage_lots, "lineage input packed lot")

    fda_response = client.get(
        "/api/mock/regengine/export/fda-request?preset=lot_trace&traceability_lot_code=TLC-DEMO-FC-OUT-001",
        headers=main_headers,
    )
    assert_status(fda_response, 200)
    assert_in("BATCH-DEMO-FC-001", fda_response.text, "FDA lot trace batch reference")

    epcis = assert_json(
        client.get(
            "/api/mock/regengine/export/epcis?traceability_lot_code=TLC-DEMO-FC-OUT-001",
            headers=main_headers,
        ),
        200,
    )
    epcis_event_types = {event["type"] for event in epcis["epcisBody"]["eventList"]}
    assert_in("TransformationEvent", epcis_event_types, "EPCIS transformation event")

    save_response = assert_json(
        client.post("/api/scenario-saves/fresh_cut_processor", headers=main_headers),
        200,
    )
    assert_equal(save_response["save"]["record_count"], 13, "saved scenario record count")

    assert_status(
        client.post(
            "/api/simulate/reset",
            headers=main_headers,
            json={
                "scenario": "retailer_readiness_demo",
                "batch_size": 1,
                "seed": 204,
                "delivery": {"mode": "none"},
            },
        ),
        200,
    )
    step = assert_json(client.post("/api/simulate/step", headers=main_headers), 200)
    assert_equal(step["generated"], 1, "single step generated count")

    load_response = assert_json(
        client.post("/api/scenario-saves/fresh_cut_processor/load", headers=main_headers),
        200,
    )
    assert_equal(load_response["loaded_records"], 13, "loaded scenario record count")

    replay = assert_json(
        client.post("/api/simulate/replay", headers=main_headers, json={"delivery": {"mode": "none"}}),
        200,
    )
    assert_equal(replay["status"], "rebuilt", "replay status")
    assert_equal(replay["replayed"], 13, "replay event count")

    assert_status(
        client.post(
            "/api/simulate/reset",
            headers=other_headers,
            json={
                "scenario": "leafy_greens_supplier",
                "batch_size": 1,
                "seed": 204,
                "delivery": {"mode": "none"},
            },
        ),
        200,
    )
    other_status = assert_json(client.get("/api/simulate/status", headers=other_headers), 200)
    main_status = assert_json(client.get("/api/simulate/status", headers=main_headers), 200)
    assert_equal(other_status["stats"]["total_records"], 0, "other tenant empty event log")
    assert_equal(main_status["stats"]["total_records"], 13, "main tenant preserved event log")

    assert_status(client.post("/api/simulate/stop", headers=main_headers), 200)
    assert_status(client.post("/api/simulate/stop", headers=other_headers), 200)


def request_headers(tenant_id: str) -> dict[str, str]:
    headers = {"X-RegEngine-Tenant": tenant_id}
    username = os.getenv("REGENGINE_BASIC_AUTH_USERNAME")
    password = os.getenv("REGENGINE_BASIC_AUTH_PASSWORD")
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def cleanup_smoke_tenants() -> None:
    for tenant_id in TENANTS:
        shutil.rmtree(tenant_dir(tenant_id), ignore_errors=True)


# _smoke_common holds the one implementation of this harness, shared with
# the other smoke scripts (#139). It takes the failure type first so each
# script keeps its own exception class; bind it once rather than at every
# call site.
assert_status = partial(_smoke_common.assert_status, SmokeFailure)
assert_equal = partial(_smoke_common.assert_equal, SmokeFailure)
assert_in = partial(_smoke_common.assert_in, SmokeFailure)


def assert_json(response, expected_status: int) -> dict[str, Any]:
    return _smoke_common.response_json(
        SmokeFailure, response, "response", expected_status=expected_status
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"Release smoke regression failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

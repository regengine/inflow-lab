from __future__ import annotations

import base64
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Imported after the sys.path bootstrap above so
# `python scripts/smoke_regression.py` works from a clean checkout; hence the
# E402 waivers.
from app.main import app  # noqa: E402
from app.tenancy import tenant_dir, tenant_events_path  # noqa: E402
from scripts import _smoke_common as smoke  # noqa: E402


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
    # Derived from app.tenancy rather than written out, because the app
    # resolves the data root from REGENGINE_DATA_DIR at import time. A literal
    # made this assertion fail -- on a cosmetic path string, saying nothing
    # about correctness -- for any operator whose shell exports that variable,
    # which DEPLOYMENT_PROFILES.md tells them to do.
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
    """Remove the smoke tenants from the data root the app actually used.

    Hardcoding ``data/`` deleted the wrong directory whenever
    REGENGINE_DATA_DIR pointed elsewhere, leaving release-smoke-main and
    release-smoke-other in the shared demo's persistent store, where
    ``known_tenant_ids()`` then surfaced them in /api/operator/tenants forever.
    """
    for tenant_id in TENANTS:
        shutil.rmtree(tenant_dir(tenant_id), ignore_errors=True)


def assert_json(response, expected_status: int) -> dict[str, Any]:
    assert_status(response, expected_status)
    return smoke.response_json(response, response.request.url.path, failure=SmokeFailure)


def assert_status(response, expected_status: int) -> None:
    smoke.assert_status(
        response,
        expected_status,
        f"{response.request.method} {response.request.url.path}",
        failure=SmokeFailure,
        redact=redact,
    )


def redact(value: str) -> str:
    """Scrub anything credential-shaped in the environment out of a body.

    The release smoke runs in-process against a TestClient, so an echoed 500
    body can carry whatever REGENGINE_BASIC_AUTH_PASSWORD (or any other
    credential env var) is set to on the operator's machine.
    """
    return smoke.redact_secrets(value, sorted(smoke.secret_values()))


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    smoke.assert_equal(actual, expected, label, failure=SmokeFailure)


def assert_in(member: Any, container: Any, label: str) -> None:
    smoke.assert_in(member, container, label, failure=SmokeFailure)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"Release smoke regression failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

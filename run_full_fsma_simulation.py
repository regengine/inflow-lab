from __future__ import annotations

import base64
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.cte_rules import validate_event_kdes
from app.main import app
from app.models import RegEngineEvent


REPO_ROOT = Path(__file__).resolve().parent
TENANT_ID = "golden-path-demo"
LOT_CODE = "TLC-DEMO-FC-OUT-001"


class SimulationFailure(AssertionError):
    pass


def main() -> int:
    client = TestClient(app)
    headers = request_headers(TENANT_ID)

    try:
        reset_simulation(client, headers)
        fixture = load_fixture(client, headers)
        status = get_status(client, headers)
        events = get_events(client, headers)
        validation = validate_events(events)
        lineage = get_lineage(client, headers)
        fda_export = get_fda_export(client, headers)
        epcis_export = get_epcis_export(client, headers)
        print_report(
            fixture=fixture,
            status=status,
            events=events,
            validation=validation,
            lineage=lineage,
            fda_export=fda_export,
            epcis_export=epcis_export,
        )
    finally:
        cleanup_demo_tenant()

    return 0


def request_headers(tenant_id: str) -> dict[str, str]:
    headers = {"X-RegEngine-Tenant": tenant_id}
    username = _env("REGENGINE_BASIC_AUTH_USERNAME")
    password = _env("REGENGINE_BASIC_AUTH_PASSWORD")
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def reset_simulation(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(
        "/api/simulate/reset",
        headers=headers,
        json={
            "scenario": "fresh_cut_processor",
            "batch_size": 1,
            "seed": 204,
            "delivery": {"mode": "mock"},
        },
    )
    assert_status(response, 200)


def load_fixture(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        "/api/demo-fixtures/fresh_cut_transformation/load",
        headers=headers,
        json={
            "source": "golden-path-demo",
            "delivery": {"mode": "mock"},
        },
    )
    body = assert_json(response, 200)
    assert_equal(body["stored"], 13, "stored fixture events")
    assert_equal(body["delivery_mode"], "mock", "fixture delivery mode")
    assert_equal(body["posted"], 13, "mock-ingested events")
    return body


def get_status(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    body = assert_json(client.get("/api/simulate/status", headers=headers), 200)
    assert_equal(body["stats"]["total_records"], 13, "status total records")
    return body


def get_events(client: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    body = assert_json(client.get("/api/events?limit=100", headers=headers), 200)
    records = body["events"]
    assert_equal(len(records), 13, "event record count")
    return records


def validate_events(records: list[dict[str, Any]]) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    event_types: Counter[str] = Counter()
    for record in records:
        event = RegEngineEvent.model_validate(record["event"])
        event_types[event.cte_type.value] += 1
        warnings.extend(
            {"lot": event.traceability_lot_code, "field": warning.field, "message": warning.message}
            for warning in validate_event_kdes(event)
        )

    return {
        "event_types": dict(sorted(event_types.items())),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def get_lineage(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    body = assert_json(client.get(f"/api/lineage/{LOT_CODE}", headers=headers), 200)
    lot_codes = {record["event"]["traceability_lot_code"] for record in body["records"]}
    assert_in("TLC-DEMO-FC-HARVEST-001", lot_codes, "upstream harvest lot")
    assert_in("TLC-DEMO-FC-PACK-001", lot_codes, "input packed lot")
    assert_in(LOT_CODE, lot_codes, "output transformed lot")
    return body


def get_fda_export(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get(
        f"/api/mock/regengine/export/fda-request?preset=lot_trace&traceability_lot_code={LOT_CODE}",
        headers=headers,
    )
    assert_status(response, 200)
    assert_in("BATCH-DEMO-FC-001", response.text, "FDA batch reference")
    return response.text


def get_epcis_export(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    body = assert_json(
        client.get(
            f"/api/mock/regengine/export/epcis?traceability_lot_code={LOT_CODE}",
            headers=headers,
        ),
        200,
    )
    event_types = {event["type"] for event in body["epcisBody"]["eventList"]}
    assert_in("TransformationEvent", event_types, "EPCIS transformation event")
    return body


def print_report(
    *,
    fixture: dict[str, Any],
    status: dict[str, Any],
    events: list[dict[str, Any]],
    validation: dict[str, Any],
    lineage: dict[str, Any],
    fda_export: str,
    epcis_export: dict[str, Any],
) -> None:
    fda_rows = max(0, len(fda_export.splitlines()) - 1)
    epcis_events = len(epcis_export["epcisBody"]["eventList"])

    print("RegEngine Inflow Lab - Full FSMA Simulation")
    print("=" * 47)
    print(f"tenant: {TENANT_ID}")
    print(f"scenario: {status['config']['scenario']}")
    print(f"fixture: {fixture['fixture_id']}")
    print()
    print("Events generated")
    print(f"- stored records: {fixture['stored']}")
    print(f"- event records returned: {len(events)}")
    print(f"- event types: {validation['event_types']}")
    print()
    print("Ingestion triggered")
    print(f"- destination mode: {fixture['delivery_mode']}")
    print(f"- mock accepted events: {fixture['posted']}")
    print()
    print("Validation results")
    print(f"- KDE warnings: {validation['warning_count']}")
    if validation["warnings"]:
        for warning in validation["warnings"][:10]:
            print(f"  - {warning['lot']}: {warning['field']} - {warning['message']}")
    else:
        print("- all fixture events satisfy required and recommended simulator KDE checks")
    print()
    print("Traceability")
    print(f"- target lot: {LOT_CODE}")
    print(f"- lineage records: {len(lineage['records'])}")
    print()
    print("Exports produced")
    print(f"- FDA lot-trace CSV rows: {fda_rows}")
    print(f"- EPCIS event count: {epcis_events}")
    print()
    print("Golden path complete: simulate -> ingest -> validate -> trace -> export")


def cleanup_demo_tenant() -> None:
    shutil.rmtree(REPO_ROOT / "data" / "tenants" / TENANT_ID, ignore_errors=True)


def assert_json(response, expected_status: int) -> dict[str, Any]:
    assert_status(response, expected_status)
    return response.json()


def assert_status(response, expected_status: int) -> None:
    if response.status_code != expected_status:
        raise SimulationFailure(
            f"Expected status {expected_status}, got {response.status_code}: {response.text}"
        )


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise SimulationFailure(f"{label}: expected {expected!r}, got {actual!r}")


def assert_in(member: Any, container: Any, label: str) -> None:
    if member not in container:
        raise SimulationFailure(f"{label}: expected {member!r} to be present")


def _env(name: str) -> str:
    import os

    return os.getenv(name, "")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimulationFailure as exc:
        print(f"Full FSMA simulation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

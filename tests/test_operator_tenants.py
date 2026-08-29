"""Tenant header scoping, data-root confinement of persist_path, and the
operator tenant-admin routes (split out of tests/test_api.py for #132).
"""

import pytest

from tests.support.api_client import basic_auth_header, client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_tenant_header_scopes_event_storage_and_rejects_invalid_ids(tmp_path):
    alpha_headers = {"X-RegEngine-Tenant": "tenant-alpha"}
    beta_headers = {"X-RegEngine-Tenant": "tenant-beta"}
    alpha_path = tmp_path / "alpha-escape-attempt.jsonl"

    reset_response = client.post(
        "/api/simulate/reset",
        headers=alpha_headers,
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(alpha_path),
            "delivery": {"mode": "none"},
        },
    )
    assert reset_response.status_code == 200
    assert client.post(
        "/api/simulate/reset",
        headers=beta_headers,
        json={"batch_size": 1, "seed": 204, "delivery": {"mode": "none"}},
    ).status_code == 200

    step_response = client.post("/api/simulate/step", headers=alpha_headers)
    assert step_response.status_code == 200

    alpha_status = client.get("/api/simulate/status", headers=alpha_headers).json()
    beta_status = client.get("/api/simulate/status", headers=beta_headers).json()
    assert alpha_status["stats"]["total_records"] == 1
    assert beta_status["stats"]["total_records"] == 0
    assert alpha_status["config"]["persist_path"] == "data/tenants/tenant-alpha/events.jsonl"
    assert beta_status["config"]["persist_path"] == "data/tenants/tenant-beta/events.jsonl"
    assert not alpha_path.exists()

    alpha_events = client.get("/api/events", headers=alpha_headers).json()["events"]
    beta_events = client.get("/api/events", headers=beta_headers).json()["events"]
    assert len(alpha_events) == 1
    assert beta_events == []

    invalid_response = client.get("/api/health", headers={"X-RegEngine-Tenant": "../tenant"})
    assert invalid_response.status_code == 400


@pytest.mark.parametrize(
    "escape_path",
    [
        "/etc/passwd",
        "../../etc/cron.d/inflow",
        "data/../../../tmp/escape.jsonl",
    ],
)
def test_default_mode_rejects_persist_path_outside_data_root(escape_path):
    # In default (no-auth) local mode the caller's persist_path is used
    # verbatim by the EventStore; a path escaping the data root must be
    # rejected (would otherwise be arbitrary file read/write).
    start_response = client.post(
        "/api/simulate/start",
        json={"config": {"batch_size": 1, "seed": 204, "persist_path": escape_path}},
    )
    assert start_response.status_code == 400

    replay_response = client.post(
        "/api/simulate/replay",
        json={"persist_path": escape_path},
    )
    assert replay_response.status_code == 400


def test_default_data_root_is_used_for_local_and_tenant_paths():
    health = client.get("/api/health").json()
    assert health["status"]["config"]["persist_path"] == "data/events.jsonl"

    tenant_health = client.get("/api/health", headers={"X-RegEngine-Tenant": "tenant-path-check"}).json()
    assert tenant_health["status"]["config"]["persist_path"] == (
        "data/tenants/tenant-path-check/events.jsonl"
    )


def test_operator_tenant_routes_require_basic_auth(monkeypatch):
    disabled = client.get("/api/operator/tenants")
    assert disabled.status_code == 403
    assert disabled.json()["detail"] == "Tenant operations require Basic Auth"

    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")

    unauthorized = client.get("/api/operator/tenants")
    assert unauthorized.status_code == 401

    authorized = client.get(
        "/api/operator/tenants",
        headers=basic_auth_header("demo-user", "demo-pass"),
    )
    assert authorized.status_code == 200
    assert "tenants" in authorized.json()


def test_operator_can_list_reset_and_delete_tenant_state(monkeypatch):
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
    operator_headers = basic_auth_header("demo-user", "demo-pass")
    tenant_id = "operator-tenant-alpha"
    tenant_headers = operator_headers | {"X-RegEngine-Tenant": tenant_id}

    reset = client.post(
        "/api/simulate/reset",
        headers=tenant_headers,
        json={"batch_size": 3, "seed": 204, "delivery": {"mode": "none"}},
    )
    assert reset.status_code == 200
    assert client.post("/api/simulate/step", headers=tenant_headers).status_code == 200

    list_response = client.get("/api/operator/tenants", headers=operator_headers)
    assert list_response.status_code == 200
    tenants = {
        tenant["tenant_id"]: tenant for tenant in list_response.json()["tenants"]
    }
    assert tenants[tenant_id]["cached"] is True
    assert tenants[tenant_id]["running"] is False
    assert tenants[tenant_id]["total_records"] == 3
    assert tenants[tenant_id]["persist_path"] == f"data/tenants/{tenant_id}/events.jsonl"
    assert tenants[tenant_id]["exists_on_disk"] is True

    tenant_reset = client.post(
        f"/api/operator/tenants/{tenant_id}/reset",
        headers=operator_headers,
    )
    assert tenant_reset.status_code == 200
    assert tenant_reset.json() == {
        "status": "reset",
        "tenant_id": tenant_id,
        "removed_cached_controller": False,
        "removed_data": False,
    }
    status = client.get("/api/simulate/status", headers=tenant_headers).json()
    assert status["stats"]["total_records"] == 0

    assert client.post("/api/simulate/step", headers=tenant_headers).status_code == 200
    tenant_delete = client.delete(
        f"/api/operator/tenants/{tenant_id}",
        headers=operator_headers,
    )
    assert tenant_delete.status_code == 200
    assert tenant_delete.json() == {
        "status": "deleted",
        "tenant_id": tenant_id,
        "removed_cached_controller": True,
        "removed_data": True,
    }
    list_after_delete = client.get("/api/operator/tenants", headers=operator_headers)
    tenant_ids = {tenant["tenant_id"] for tenant in list_after_delete.json()["tenants"]}
    assert tenant_id not in tenant_ids

    default_delete = client.delete("/api/operator/tenants/local-demo", headers=operator_headers)
    assert default_delete.status_code == 400

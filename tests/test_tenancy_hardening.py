"""Regression tests for tenancy and CORS hardening.

Covers:
- #174 unauthenticated requests minting unbounded tenant controllers/directories
- #175 the operator-delete race that left a zombie tenant controller
- #178 one malformed REGENGINE_CORS_ORIGINS entry crashing app startup
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import tenancy
from app.auth import DEFAULT_TENANT_ID
from app.cors import DEFAULT_CORS_ORIGINS, cors_origins_for_app, cors_origins_from_env
from app.main import app


client = TestClient(app)


@pytest.fixture
def isolated_tenants(monkeypatch, tmp_path):
    """Point tenant storage and the controller registry at empty scratch state."""
    tenant_root = tmp_path / "tenants"
    tenant_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", tenant_root)
    monkeypatch.setattr(
        tenancy, "_tenant_controllers", {DEFAULT_TENANT_ID: tenancy.controller}
    )
    monkeypatch.setattr(tenancy, "_tenants_being_deleted", {})
    return tenant_root


# --------------------------------------------------------------------------
# #174 — unauthenticated tenant flood
# --------------------------------------------------------------------------


def test_unauthenticated_tenant_flood_is_capped(isolated_tenants, monkeypatch):
    monkeypatch.setenv("REGENGINE_MAX_TENANTS", "3")

    accepted: list[str] = []
    refused: list[str] = []
    for index in range(25):
        tenant_id = f"flood-{index}"
        response = client.get("/api/health", headers={"X-RegEngine-Tenant": tenant_id})
        if response.status_code == 200:
            accepted.append(tenant_id)
        else:
            assert response.status_code == 429
            assert "capacity" in response.json()["detail"].lower()
            refused.append(tenant_id)

    assert len(accepted) == 3
    assert len(refused) == 22

    tenant_dirs = [path.name for path in isolated_tenants.iterdir() if path.is_dir()]
    assert sorted(tenant_dirs) == sorted(accepted)
    cached = set(tenancy._tenant_controllers) - {DEFAULT_TENANT_ID}
    assert cached == set(accepted)


def test_capped_tenants_keep_serving_existing_tenants(isolated_tenants, monkeypatch):
    monkeypatch.setenv("REGENGINE_MAX_TENANTS", "1")

    first = client.get("/api/health", headers={"X-RegEngine-Tenant": "kept-tenant"})
    assert first.status_code == 200

    assert client.get("/api/health", headers={"X-RegEngine-Tenant": "extra"}).status_code == 429

    # The tenant that already exists is unaffected by the cap.
    again = client.get("/api/health", headers={"X-RegEngine-Tenant": "kept-tenant"})
    assert again.status_code == 200
    assert again.json()["tenant"] == "kept-tenant"

    # So is the built-in default tenant, so a flood cannot deny normal service.
    assert client.get("/api/health").status_code == 200


def test_tenant_cap_counts_directories_left_on_disk(isolated_tenants, monkeypatch):
    (isolated_tenants / "from-a-previous-run").mkdir()
    monkeypatch.setenv("REGENGINE_MAX_TENANTS", "1")

    assert client.get("/api/health", headers={"X-RegEngine-Tenant": "new-one"}).status_code == 429
    # The pre-existing tenant is still reachable.
    assert (
        client.get("/api/health", headers={"X-RegEngine-Tenant": "from-a-previous-run"}).status_code
        == 200
    )


@pytest.mark.parametrize("raw_limit", ["", "not-a-number", "0", "-5"])
def test_malformed_max_tenants_falls_back_to_default(monkeypatch, raw_limit):
    monkeypatch.setenv("REGENGINE_MAX_TENANTS", raw_limit)
    assert tenancy.max_tenant_count() == tenancy.DEFAULT_MAX_TENANTS


# --------------------------------------------------------------------------
# #175 — operator delete race
# --------------------------------------------------------------------------


def test_request_during_delete_cannot_zombie_a_tenant(isolated_tenants):
    tenant_id = "race-tenant"
    tenancy.get_tenant_controller_for_id(tenant_id)
    assert tenancy.tenant_dir(tenant_id).exists()

    # Interleave exactly where the router's race window is: after the pop,
    # before the directory removal.
    popped = tenancy.pop_tenant_controller(tenant_id)
    assert popped is not None

    with pytest.raises(HTTPException) as excinfo:
        tenancy.get_tenant_controller_for_id(tenant_id)
    assert excinfo.value.status_code == 409

    import shutil

    shutil.rmtree(tenancy.tenant_dir(tenant_id), ignore_errors=True)

    # Fully absent, not zombied: no cached controller pointing at a missing dir.
    assert tenant_id not in tenancy._tenant_controllers
    assert not tenancy.tenant_dir(tenant_id).exists()

    # And the tenant self-heals on the next request once the delete finished.
    recreated = tenancy.get_tenant_controller_for_id(tenant_id)
    assert recreated is not popped
    assert tenancy.tenant_dir(tenant_id).exists()


def test_delete_tenant_is_atomic_and_repeatable(isolated_tenants):
    tenant_id = "atomic-delete"
    tenancy.get_tenant_controller_for_id(tenant_id)

    removed_controller, removed_data = asyncio.run(tenancy.delete_tenant(tenant_id))
    assert removed_controller is True
    assert removed_data is True
    assert tenant_id not in tenancy._tenant_controllers
    assert not tenancy.tenant_dir(tenant_id).exists()
    assert tenant_id not in tenancy._tenants_being_deleted

    # A second delete stays a safe cleanup path.
    removed_controller, removed_data = asyncio.run(tenancy.delete_tenant(tenant_id))
    assert removed_controller is False
    assert removed_data is False

    # Reset-style re-creation after a delete still works (self-heal path).
    assert tenancy.get_tenant_controller_for_id(tenant_id) is not None


def test_delete_quarantine_expires_if_removal_never_completes(isolated_tenants, monkeypatch):
    tenant_id = "wedged-delete"
    tenancy.get_tenant_controller_for_id(tenant_id)
    tenancy.pop_tenant_controller(tenant_id)

    # Directory still present (rmtree failed/never ran) -> creation refused.
    with pytest.raises(HTTPException):
        tenancy.get_tenant_controller_for_id(tenant_id)

    # ...but the quarantine cannot wedge the tenant forever.
    monkeypatch.setattr(tenancy, "_DELETE_QUARANTINE_SECONDS", -1.0)
    assert tenancy.get_tenant_controller_for_id(tenant_id) is not None
    assert tenant_id not in tenancy._tenants_being_deleted


def test_operator_delete_over_http_still_removes_tenant(isolated_tenants, monkeypatch):
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "ops")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "ops-secret")
    import base64

    token = base64.b64encode(b"ops:ops-secret").decode()
    operator_headers = {"Authorization": f"Basic {token}"}
    tenant_id = "http-delete-tenant"
    tenant_headers = operator_headers | {"X-RegEngine-Tenant": tenant_id}

    assert client.get("/api/health", headers=tenant_headers).status_code == 200

    deleted = client.delete(f"/api/operator/tenants/{tenant_id}", headers=operator_headers)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert tenant_id not in tenancy._tenant_controllers
    assert not tenancy.tenant_dir(tenant_id).exists()

    # Reset after delete remains a working self-heal path.
    reset = client.post(f"/api/operator/tenants/{tenant_id}/reset", headers=operator_headers)
    assert reset.status_code == 200


# --------------------------------------------------------------------------
# #178 — malformed REGENGINE_CORS_ORIGINS must not crash startup
# --------------------------------------------------------------------------


def test_malformed_cors_origins_do_not_crash_app_construction(monkeypatch, caplog):
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setenv(
        "REGENGINE_CORS_ORIGINS", "https://good.example.com, demo.example.com"
    )

    # The strict parser still raises for direct callers...
    with pytest.raises(ValueError):
        cors_origins_from_env()

    # ...but the app-construction path degrades instead of taking the process down.
    with caplog.at_level(logging.WARNING, logger="app.cors"):
        origins = cors_origins_for_app()
    assert origins == list(DEFAULT_CORS_ORIGINS)
    assert "REGENGINE_CORS_ORIGINS" in caplog.text
    assert "demo.example.com" in caplog.text


def test_create_app_survives_malformed_cors_origins(monkeypatch):
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://good.example.com, demo.example.com")
    from app.main import create_app

    assert create_app() is not None


def test_malformed_cors_origins_still_trust_the_platform_domain(monkeypatch):
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "*")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "demo.up.railway.app")
    assert cors_origins_for_app() == [
        *DEFAULT_CORS_ORIGINS,
        "https://demo.up.railway.app",
    ]


def test_valid_cors_origins_pass_through_unchanged(monkeypatch):
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://demo.example.com")
    assert cors_origins_for_app() == ["https://demo.example.com"]

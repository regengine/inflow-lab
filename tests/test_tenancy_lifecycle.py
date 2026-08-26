"""Regression tests for the tenant-controller lifecycle (#174, #175).

#174: resolving any non-default `X-RegEngine-Tenant` header lazily mints a
full SimulationController and an on-disk directory, with no auth required to
pick an id and (before this fix) no cap. These tests drive many distinct
tenant headers -- with zero credentials, matching the issue's own repro --
and assert growth of both `tenancy._tenant_controllers` and
`tenancy.TENANT_DATA_ROOT` stops at `tenancy.MAX_TENANT_CONTROLLERS`, and
that the single shared `local-demo` tenant the no-auth flow depends on is
never subject to the cap.

#175: `DELETE /api/operator/tenants/{id}` pops the controller and only then
`shutil.rmtree`s its directory, as separate, unlocked steps. A request for
the same tenant landing in that window used to re-create the controller and
then crash with an unhandled FileNotFoundError on its next write. These
tests interleave a `get_tenant_controller_for_id` call in that exact window
-- the same reproduction method the issue itself used -- and confirm the
tenant ends up fully absent rather than zombied, and that the request that
loses the race gets a clean error instead of a 500.
"""

from __future__ import annotations

import base64
import shutil

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import tenancy
from app.main import app


client = TestClient(app)


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _cleanup_tenant_dir(tenant_id: str) -> None:
    shutil.rmtree(tenancy.tenant_dir(tenant_id), ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_tenant_registry(monkeypatch):
    """Give every test in this file its own tenant registry.

    `_tenant_controllers` and `_tenants_being_deleted` are module-level
    globals shared with the rest of the test suite (and the running app).
    Swapping in fresh containers -- monkeypatch restores the originals after
    each test -- makes the cap assertions below exact regardless of what
    other test files created before this one runs, and stops this file's own
    tenant floods from leaking entries into the rest of the session.
    """
    monkeypatch.setattr(tenancy, "_tenant_controllers", {tenancy.DEFAULT_TENANT_ID: tenancy.controller})
    monkeypatch.setattr(tenancy, "_tenants_being_deleted", set())
    yield


# ---------------------------------------------------------------------------
# #174 -- unauthenticated tenant-controller creation is capped, not unbounded
# ---------------------------------------------------------------------------


def test_unauthenticated_header_flood_is_capped_not_unbounded(monkeypatch):
    """Mirrors the issue's own repro: many distinct tenant headers, zero
    credentials. Growth of both the in-memory registry and the on-disk
    tenant directories must stop at the cap instead of tracking 1:1 with
    the number of distinct headers sent."""
    monkeypatch.setattr(tenancy, "MAX_TENANT_CONTROLLERS", 5)
    tenant_ids = [f"flood-tenant-{i}" for i in range(30)]

    try:
        statuses = [
            client.get("/api/health", headers={"X-RegEngine-Tenant": tenant_id}).status_code
            for tenant_id in tenant_ids
        ]

        # Registry started at 1 (the seeded default); cap is 5, so exactly
        # 4 of the 30 distinct, never-seen-before ids can mint a controller.
        assert statuses.count(200) == 4
        assert statuses.count(429) == 26
        assert len(tenancy._tenant_controllers) == 5

        # The refused requests must never have touched disk either -- only
        # the first 4 tenant ids may have a directory.
        on_disk = {tenant_id for tenant_id in tenant_ids if tenancy.tenant_dir(tenant_id).exists()}
        assert on_disk == set(tenant_ids[:4])
    finally:
        for tenant_id in tenant_ids:
            _cleanup_tenant_dir(tenant_id)


def test_capped_request_gets_clear_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(tenancy, "MAX_TENANT_CONTROLLERS", 2)

    try:
        # Seeded default (1) + this one (2) exactly fills the cap.
        first = client.get("/api/health", headers={"X-RegEngine-Tenant": "cap-fill-1"})
        assert first.status_code == 200

        # A second, distinct id is refused with a clear, structured error --
        # never a 500 -- and never creates anything.
        second = client.get("/api/health", headers={"X-RegEngine-Tenant": "cap-fill-2"})
        assert second.status_code == 429
        assert "capacity" in second.json()["detail"].lower()
        assert not tenancy.tenant_dir("cap-fill-2").exists()
        assert "cap-fill-2" not in tenancy._tenant_controllers
    finally:
        _cleanup_tenant_dir("cap-fill-1")
        _cleanup_tenant_dir("cap-fill-2")


def test_cap_applies_regardless_of_auth_state(monkeypatch):
    """The proposed fix bounds `_tenant_controllers` "regardless of auth
    state" -- an authenticated caller can still be refused once the cap is
    hit, since only an operator reset/delete frees a slot, not merely
    presenting credentials."""
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
    monkeypatch.setattr(tenancy, "MAX_TENANT_CONTROLLERS", 2)
    auth_headers = basic_auth_header("demo-user", "demo-pass")

    try:
        first = client.get("/api/health", headers=auth_headers | {"X-RegEngine-Tenant": "auth-cap-1"})
        assert first.status_code == 200

        second = client.get("/api/health", headers=auth_headers | {"X-RegEngine-Tenant": "auth-cap-2"})
        assert second.status_code == 429
    finally:
        _cleanup_tenant_dir("auth-cap-1")
        _cleanup_tenant_dir("auth-cap-2")


def test_default_local_demo_tenant_is_never_capped(monkeypatch):
    """The no-auth local flow's single shared tenant must keep working no
    matter how exhausted the cap is: it is served straight from
    `active_controller_for_context`'s `uses_default_storage` fast path and
    never goes through `get_tenant_controller_for_id` (and its cap check)
    at all. A cap of 0 is the most extreme case."""
    monkeypatch.setattr(tenancy, "MAX_TENANT_CONTROLLERS", 0)

    for _ in range(10):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["tenant"] == tenancy.DEFAULT_TENANT_ID

    # Explicitly asking for the default tenant id by name, still no auth,
    # takes the same exempt path.
    explicit_default = client.get("/api/health", headers={"X-RegEngine-Tenant": tenancy.DEFAULT_TENANT_ID})
    assert explicit_default.status_code == 200


# ---------------------------------------------------------------------------
# #175 -- operator-delete race no longer leaves a zombie controller or a 500
# ---------------------------------------------------------------------------


def test_race_window_refuses_recreation_instead_of_zombie_controller():
    """Direct reproduction, matching the issue's own method: interleave a
    `get_tenant_controller_for_id` call between `pop_tenant_controller` and
    the directory removal (operator.py's delete does exactly this
    sequence)."""
    tenant_id = "race-tenant-alpha"

    try:
        original = tenancy.get_tenant_controller_for_id(tenant_id)
        assert tenancy.tenant_dir(tenant_id).exists()

        # operator.py:45 -- pop from the registry, opening the delete window.
        popped = tenancy.pop_tenant_controller(tenant_id)
        assert popped is original

        # A request lands in the window between the pop and the rmtree
        # (operator.py:45-49, before this fix). It must not resurrect the
        # tenant or touch disk -- just a clean, retryable error.
        with pytest.raises(HTTPException) as exc_info:
            tenancy.get_tenant_controller_for_id(tenant_id)
        assert exc_info.value.status_code == 409
        assert tenant_id not in tenancy._tenant_controllers

        # operator.py:49 -- the delete's rmtree, now safe to run since
        # nothing recreated the directory out from under it.
        shutil.rmtree(tenancy.tenant_dir(tenant_id), ignore_errors=True)
        tenancy.finish_tenant_delete(tenant_id)

        # Fully absent, not zombied: no cached controller, no directory.
        assert tenant_id not in tenancy._tenant_controllers
        assert not tenancy.tenant_dir(tenant_id).exists()

        # The delete window is closed once finish_tenant_delete runs -- an
        # ordinary request afterward must self-heal cleanly, not stay
        # refused forever.
        fresh = tenancy.get_tenant_controller_for_id(tenant_id)
        assert fresh is not original
        assert tenancy.tenant_dir(tenant_id).exists()
    finally:
        _cleanup_tenant_dir(tenant_id)


def test_delete_endpoint_race_returns_sane_error_not_500(monkeypatch):
    """End-to-end through the real DELETE route. A request landing between
    the pop and the rmtree must get a clean error instead of racing the
    rmtree, the DELETE itself must still succeed and leave the tenant fully
    gone, and both reset and a second DELETE remain safe afterward."""
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
    operator_headers = basic_auth_header("demo-user", "demo-pass")
    tenant_id = "race-tenant-http"
    tenant_headers = operator_headers | {"X-RegEngine-Tenant": tenant_id}

    try:
        seed = client.post(
            "/api/simulate/reset",
            headers=tenant_headers,
            json={"batch_size": 1, "seed": 204, "delivery": {"mode": "none"}},
        )
        assert seed.status_code == 200
        assert tenancy.tenant_dir(tenant_id).exists()

        real_rmtree = shutil.rmtree
        racer_error: dict[str, HTTPException] = {}

        def racing_rmtree(path, *args, **kwargs):
            # Fires exactly in the operator.py delete-route window: the
            # controller has already been popped, but the directory has not
            # been removed yet. A plain, synchronous tenancy call here is
            # exactly what a concurrent request's dependency resolution
            # would do -- no TestClient re-entrancy needed to prove it.
            try:
                tenancy.get_tenant_controller_for_id(tenant_id)
            except HTTPException as exc:
                racer_error["exc"] = exc
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", racing_rmtree)
        try:
            delete_response = client.delete(
                f"/api/operator/tenants/{tenant_id}",
                headers=operator_headers,
            )
        finally:
            monkeypatch.setattr(shutil, "rmtree", real_rmtree)

        assert delete_response.status_code == 200
        assert delete_response.json()["status"] == "deleted"

        # The racer lost cleanly: a structured 409, never an unhandled
        # FileNotFoundError/500.
        assert racer_error.get("exc") is not None
        assert racer_error["exc"].status_code == 409

        # No zombie: absent from the registry and off disk.
        assert tenant_id not in tenancy._tenant_controllers
        assert not tenancy.tenant_dir(tenant_id).exists()

        # Self-heal path #1: reset after delete recreates the tenant cleanly.
        reset_after = client.post(f"/api/operator/tenants/{tenant_id}/reset", headers=operator_headers)
        assert reset_after.status_code == 200

        # Self-heal path #2: a second delete remains safe and idempotent.
        second_delete = client.delete(f"/api/operator/tenants/{tenant_id}", headers=operator_headers)
        assert second_delete.status_code == 200
        assert second_delete.json()["removed_cached_controller"] is True

        # And an ordinary write for the tenant afterward works cleanly --
        # no lingering 500 from the now-closed race window.
        after = client.post("/api/simulate/step", headers=tenant_headers)
        assert after.status_code == 200
    finally:
        _cleanup_tenant_dir(tenant_id)

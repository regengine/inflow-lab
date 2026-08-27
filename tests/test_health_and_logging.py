"""Tests for #184 (``/api/healthz`` must fail when the event store can't
actually write) and the store-write-failure logging slice of #182 that this
agent's owned files (``app/routers/health.py``, ``app/main.py``,
``app/build_info.py``) can cover directly.

Both issues were originally reproduced the same way: point the default
tenant's ``EventStore.persist_path`` at ``/dev/full`` (a device that accepts
every ``open()`` and fails every ``write()`` with ENOSPC) and show that
``/api/healthz`` kept answering ``200 {"ok": true}`` while zero log records
were emitted anywhere in the process. These tests reproduce that exact setup
against the real app -- the same technique ``tests/test_store_durability.py``
already uses for ``EventStore`` directly.

Delivery-failure and tenant-creation logging (the other two thirds of #182)
live in ``app/controller.py`` and ``app/tenancy.py``, which this agent does
not own; see the task report for the exact call sites to wire up there.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, controller
from app.routers.health import _store_write_error
from app.store import EventStore


client = TestClient(app)


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------------------
# _store_write_error -- the check function in isolation
# ---------------------------------------------------------------------------


def test_store_write_error_probe_never_produces_a_phantom_record(tmp_path):
    """The probe must be able to write (happy path), must never show up as a
    record, and must never touch the tenant's event log at all.

    This previously asserted ``persist_path.read_text() == "\n\n"`` -- it
    documented the probe appending a blank line to the real event file on
    every poll, rather than bounding it. That is an unauthenticated, unbounded
    append to tenant data: the Docker ``HEALTHCHECK --interval=30s`` alone
    grows the persistent volume forever, and an unauthenticated caller can
    drive it far faster. The probe now writes a truncating sentinel beside the
    log instead, so the assertions below are the stronger property: the event
    log is left byte-for-byte untouched, and the sentinel cannot grow no
    matter how many times it is polled.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    persist_path.write_text("", encoding="utf-8")
    before = persist_path.read_bytes()

    for _ in range(25):
        assert _store_write_error(store) is None

    assert persist_path.read_bytes() == before, "the health probe wrote into the event log"
    assert store.read_persisted_records() == []

    probe_path = persist_path.parent / ".healthz-write-probe"
    assert probe_path.is_file(), "the probe must actually have written something"
    assert probe_path.stat().st_size <= len("ok\n"), (
        f"probe file grew across polls: {probe_path.stat().st_size} bytes"
    )


def test_store_write_error_detects_a_write_that_cannot_land_on_disk(tmp_path):
    """The exact failure #184 was filed against: persist_path pointed at
    /dev/full, which accepts every open() and fails every write() with
    ENOSPC. A permission-bits-only check (os.access) would miss this --
    /dev/full reports as writable -- which is why this has to be a real
    write, not a stat.
    """
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.persist_path = Path("/dev/full")

    error = _store_write_error(store)

    assert error is not None
    assert "28" in error or "space" in error.lower()


# ---------------------------------------------------------------------------
# /api/healthz -- the unauthenticated endpoint Docker's HEALTHCHECK and
# railway.json's healthcheckPath / restartPolicyType actually poll (#184)
# ---------------------------------------------------------------------------


def test_healthz_returns_200_on_the_happy_path():
    response = client.get("/api/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "error" not in body
    assert body["build"]["version"]
    assert "contract_version" in body


def test_healthz_returns_503_and_logs_when_the_store_cannot_write(monkeypatch, caplog):
    # /api/healthz always resolves the default tenant's shared controller
    # (auth_and_tenant_middleware bypasses tenant/auth resolution for this
    # exact path), so that's the store to break -- mirrors the issue's own
    # reproduction. monkeypatch restores the original persist_path
    # unconditionally once this test ends, since this is module-level state
    # every other test in the suite also shares.
    monkeypatch.setattr(controller.store, "persist_path", Path("/dev/full"))
    caplog.set_level(logging.ERROR, logger="inflow_lab")

    response = client.get("/api/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "event store is not writable"
    # Still non-secret build/status metadata only (SECURITY_BOUNDARIES.md) --
    # no raw path or OSError text leaks into the client-facing response.
    assert "/dev/full" not in response.text

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    assert any(
        "healthz failed" in message and "persist_path=/dev/full" in message for message in messages
    )


def test_health_also_fails_closed_when_the_store_cannot_write(monkeypatch, caplog):
    """The issue flagged /api/health too (it already had a real dependency,
    active_controller.status(), but still hardcoded "ok": true on top of
    it). Confirms the same fix covers the tenant-scoped endpoint the
    console UI uses, not just the platform healthcheck.
    """
    monkeypatch.setattr(controller.store, "persist_path", Path("/dev/full"))
    caplog.set_level(logging.ERROR, logger="inflow_lab")

    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["tenant"] == "local-demo"
    # The failure branch must short-circuit before active_controller.status()
    # -- status() reads persist_path back via EventStore.all_between(), and
    # /dev/full-style devices can stream forever on read instead of raising,
    # which would hang the request instead of failing it fast.
    assert "status" not in body

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    assert any("health check failed" in message and "tenant=local-demo" in message for message in messages)


def test_store_write_failure_log_never_contains_a_configured_secret(monkeypatch, caplog):
    """#182's masking requirement, applied to the one store-write call site
    this agent owns: a Basic Auth password is a real configured secret
    that's in scope for this exact request, so it's the meaningful
    negative case -- not just an assertion that happens to pass because no
    secret was ever nearby.
    """
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "super-secret-password")
    monkeypatch.setattr(controller.store, "persist_path", Path("/dev/full"))
    caplog.set_level(logging.INFO)

    # With Basic Auth enabled, TenantContext.uses_default_storage is False
    # even for the default tenant id (see app/auth.py), and an
    # authenticated request with no tenant header is scoped to the
    # username instead -- so this pins the request back to the shared
    # controller (the one just patched) the same way an operator would.
    headers = _basic_auth_header("demo-user", "super-secret-password") | {"X-RegEngine-Tenant": "local-demo"}
    response = client.get("/api/health", headers=headers)

    assert response.status_code == 503
    assert response.json()["ok"] is False

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret-password" not in log_text
    assert _basic_auth_header("demo-user", "super-secret-password")["Authorization"] not in log_text


# ---------------------------------------------------------------------------
# #146 — the health endpoints must publish a concrete OpenAPI schema
# ---------------------------------------------------------------------------


def test_health_endpoints_publish_concrete_openapi_schemas():
    """#146 asked for schemas that list concrete fields instead of
    ``additionalProperties: true``. Adding the 503 branch moved it backwards
    instead: the union return type forced ``response_model=None``, and the
    published schema for both routes collapsed to ``{}`` -- strictly less than
    the ``additionalProperties: true`` it replaced.
    """
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        document = client.get("/openapi.json").json()

    def resolve(ref: str) -> dict:
        assert ref.startswith("#/components/schemas/"), ref
        return document["components"]["schemas"][ref.rsplit("/", 1)[1]]

    for path, expected_fields in (
        ("/api/healthz", {"ok", "utc_time", "build", "contract_version"}),
        ("/api/health", {"ok", "utc_time", "build", "contract_version", "tenant", "auth", "status"}),
    ):
        content = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
        schema = content["schema"]
        assert schema, f"{path} publishes an empty 200 schema"
        resolved = resolve(schema["$ref"])
        assert expected_fields <= set(resolved["properties"]), (
            f"{path} 200 schema is missing fields: {expected_fields - set(resolved['properties'])}"
        )
        assert resolved.get("additionalProperties") is not True, (
            f"{path} 200 schema is still an open dict"
        )

        # The 503 branch is documented too, and it is the one that carries
        # `error` -- the healthy body has never included it.
        unavailable = document["paths"][path]["get"]["responses"]["503"]["content"]["application/json"]
        unavailable_schema = resolve(unavailable["schema"]["$ref"])
        assert "error" in unavailable_schema["properties"], f"{path} 503 schema omits `error`"

    # The build block is a named model, not a bare dict -- that was #146's
    # actual complaint about the health payload.
    build_ref = resolve(
        document["paths"]["/api/healthz"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
    )["properties"]["build"]
    assert "$ref" in build_ref or "allOf" in build_ref, "build is still an untyped object"

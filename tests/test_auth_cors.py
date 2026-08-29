"""CORS allowlisting, Basic Auth, browser-origin guarding, build metadata
and request-log redaction for the HTTP edge (split out of tests/test_api.py
for #132).
"""

import json
import logging

import pytest

from app.build_info import BRANCH_ENV_VARS, COMMIT_SHA_ENV_VARS, DEPLOYMENT_ID_ENV_VARS
from app.cors import DEFAULT_CORS_ORIGINS
from app.main import cors_origins_from_env
from tests.support.api_client import basic_auth_header, client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_cors_defaults_allow_local_origins_and_block_unknown_origins():
    allowed = client.get("/api/health", headers={"Origin": "http://127.0.0.1:8000"})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    blocked = client.get("/api/health", headers={"Origin": "https://untrusted.example"})
    assert blocked.status_code == 200
    assert "access-control-allow-origin" not in blocked.headers


def test_cors_origins_can_be_configured_without_wildcard_credentials(monkeypatch):
    monkeypatch.setenv(
        "REGENGINE_CORS_ORIGINS",
        " https://demo.example.com/, http://localhost:3000, https://demo.example.com ",
    )
    assert cors_origins_from_env() == ["https://demo.example.com", "http://localhost:3000"]

    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="cannot contain"):
        cors_origins_from_env()

    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://demo.example.com/path")
    with pytest.raises(ValueError, match="HTTP\\(S\\) origins"):
        cors_origins_from_env()


def test_cors_allowlist_always_trusts_the_platform_domain(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "demo.up.railway.app")

    # With no explicit config, the platform origin joins the local defaults.
    monkeypatch.delenv("REGENGINE_CORS_ORIGINS", raising=False)
    assert cors_origins_from_env() == [
        *DEFAULT_CORS_ORIGINS,
        "https://demo.up.railway.app",
    ]

    # An explicit-but-stale list can no longer lock the service out of its own
    # domain — the regression that kept the nightly smokes red after the
    # August 2026 cutover (issues #80/#81).
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://old.up.railway.app")
    assert cors_origins_from_env() == [
        "https://old.up.railway.app",
        "https://demo.up.railway.app",
    ]

    # No duplicate when the platform origin is already configured.
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://demo.up.railway.app")
    assert cors_origins_from_env() == ["https://demo.up.railway.app"]


def test_cors_platform_domain_never_crashes_startup(monkeypatch):
    monkeypatch.delenv("REGENGINE_CORS_ORIGINS", raising=False)

    # A malformed platform value degrades to "no extra origin", not a raise —
    # this path runs while the ASGI app is being constructed.
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "demo.up.railway.app/?bad=1")
    assert cors_origins_from_env() == list(DEFAULT_CORS_ORIGINS)

    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "   ")
    assert cors_origins_from_env() == list(DEFAULT_CORS_ORIGINS)


def test_basic_auth_is_optional_but_enforced_when_configured(monkeypatch):
    health_response = client.get("/api/health")
    assert health_response.status_code == 200
    health = health_response.json()
    assert health["tenant"] == "local-demo"
    assert health["auth"] == {
        "enabled": False,
        "username": None,
        "uses_default_storage": True,
    }

    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")

    healthz = client.get("/api/healthz")
    assert healthz.status_code == 200
    assert healthz.json()["ok"] is True

    unauthorized = client.get("/api/health")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == 'Basic realm="RegEngine Inflow Lab"'

    bad_password = client.get("/api/health", headers=basic_auth_header("demo-user", "wrong"))
    assert bad_password.status_code == 401

    authorized = client.get("/api/health", headers=basic_auth_header("demo-user", "demo-pass"))
    assert authorized.status_code == 200
    authorized_body = authorized.json()
    assert authorized_body["tenant"] == "demo-user"
    assert authorized_body["auth"] == {
        "enabled": True,
        "username": "demo-user",
        "uses_default_storage": False,
    }


def test_health_routes_include_build_metadata_from_whitelisted_env(monkeypatch):
    for name in COMMIT_SHA_ENV_VARS + BRANCH_ENV_VARS + DEPLOYMENT_ID_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGENGINE_APP_VERSION", "2.4.6")
    monkeypatch.setenv("REGENGINE_BUILD_SHA", "abcdef1234567890")
    monkeypatch.setenv("REGENGINE_BUILD_BRANCH", "codex/build-health-info")
    monkeypatch.setenv("REGENGINE_DEPLOYMENT_ID", "deploy-123")
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "should-not-appear")

    healthz = client.get("/api/healthz")
    assert healthz.status_code == 200
    healthz_build = healthz.json()["build"]
    assert healthz_build == {
        "version": "2.4.6",
        "commit_sha": "abcdef1234567890",
        "commit_sha_short": "abcdef1",
        "commit_source": "REGENGINE_BUILD_SHA",
        "branch": "codex/build-health-info",
        "branch_source": "REGENGINE_BUILD_BRANCH",
        "deployment_id": "deploy-123",
        "deployment_source": "REGENGINE_DEPLOYMENT_ID",
    }
    assert "should-not-appear" not in json.dumps(healthz.json())

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["build"] == healthz_build


def test_basic_auth_blocks_state_changes_from_untrusted_browser_origins(monkeypatch):
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
    headers = basic_auth_header("demo-user", "demo-pass") | {
        "X-RegEngine-Tenant": "origin-guard-test"
    }
    reset = client.post(
        "/api/simulate/reset",
        headers=headers,
        json={"batch_size": 3, "seed": 204, "delivery": {"mode": "none"}},
    )
    assert reset.status_code == 200

    blocked = client.post(
        "/api/simulate/step",
        headers=headers | {"Origin": "https://untrusted.example"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "State-changing requests require a trusted browser origin"

    null_origin = client.post("/api/simulate/step", headers=headers | {"Origin": "null"})
    assert null_origin.status_code == 403

    blocked_referer = client.post(
        "/api/simulate/step",
        headers=headers | {"Referer": "https://untrusted.example/form"},
    )
    assert blocked_referer.status_code == 403

    status = client.get("/api/simulate/status", headers=headers).json()
    assert status["stats"]["total_records"] == 0

    allowed_origin = client.post(
        "/api/simulate/step",
        headers=headers | {"Origin": "http://127.0.0.1:8000"},
    )
    assert allowed_origin.status_code == 200
    assert allowed_origin.json()["generated"] == 3

    script_style = client.post("/api/simulate/stop", headers=headers)
    assert script_style.status_code == 200


def test_request_logging_includes_ops_fields_without_auth_or_query_secrets(monkeypatch, caplog):
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
    caplog.set_level(logging.INFO, logger="regengine.request")

    response = client.get(
        "/api/health?api_key=query-secret",
        headers={
            **basic_auth_header("demo-user", "demo-pass"),
            "X-RegEngine-Tenant": "ops-tenant",
            "X-RegEngine-API-Key": "header-secret",
        },
    )

    assert response.status_code == 200
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "regengine.request"
    ]
    assert any(
        "method=GET" in message
        and "path=/api/health" in message
        and "status=200" in message
        and "tenant=ops-tenant" in message
        and "delivery_mode=mock" in message
        for message in messages
    )
    log_text = "\n".join(messages)
    assert "demo-pass" not in log_text
    assert "query-secret" not in log_text
    assert "header-secret" not in log_text
    assert "Authorization" not in log_text
    assert basic_auth_header("demo-user", "demo-pass")["Authorization"] not in log_text


def test_request_logging_redacts_failed_basic_auth_attempts(monkeypatch, caplog):
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
    caplog.set_level(logging.INFO, logger="regengine.request")

    response = client.get(
        "/api/health",
        headers=basic_auth_header("demo-user", "wrong-password"),
    )

    assert response.status_code == 401
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "regengine.request"
    ]
    assert any("path=/api/health" in message and "status=401" in message for message in messages)
    log_text = "\n".join(messages)
    assert "wrong-password" not in log_text
    assert basic_auth_header("demo-user", "wrong-password")["Authorization"] not in log_text

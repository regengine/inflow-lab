"""Regression tests for the startup-safety fixes in issues #88 and #178.

#88 -- a shared/remote deployment that forgets the Basic Auth env vars used
to boot anyway with only a log warning, while the container binds 0.0.0.0
unconditionally. REGENGINE_REQUIRE_AUTH now makes that fail closed.

#178 -- a single malformed REGENGINE_CORS_ORIGINS entry (a missing scheme is
the likely operator typo) used to raise out of create_app() and crash the
whole process before it bound a port. resolve_cors_origins() now degrades
that one entry instead.

These tests build their own FastAPI app via create_app() (optionally inside
`with TestClient(...)` to drive the ASGI lifespan) rather than using the
`app.main.app` singleton other test modules import -- that singleton is
constructed once, at collection time, before any of these tests get a
chance to set the environment variables under test.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import base64

from app.cors import (
    DEFAULT_CORS_ORIGINS,
    cors_origins_from_env,
    resolve_cors_origins,
    resolve_cors_origins_cached,
)
from app.main import _shared_deployment_requires_auth, create_app


def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGENGINE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("REGENGINE_BASIC_AUTH_PASSWORD", raising=False)


def _clear_cors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGENGINE_CORS_ORIGINS", raising=False)
    # A live RAILWAY_PUBLIC_DOMAIN would union an extra trusted origin in and
    # make these assertions depend on the environment running the suite.
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


def test_fails_closed_when_require_auth_set_and_basic_auth_unset(monkeypatch):
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", "1")
    _clear_auth_env(monkeypatch)

    with pytest.raises(
        RuntimeError,
        match="REGENGINE_BASIC_AUTH_USERNAME.*REGENGINE_BASIC_AUTH_PASSWORD",
    ):
        with TestClient(create_app()):
            pass


def test_starts_with_require_auth_set_and_basic_auth_configured(monkeypatch):
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", "1")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")

    with TestClient(create_app()) as client:
        # /api/healthz is the unauthenticated Docker/Railway healthcheck
        # target, so it is the right probe that the app actually came up.
        response = client.get("/api/healthz")
        assert response.status_code == 200


def test_local_opt_out_still_starts_without_auth(monkeypatch, caplog):
    # REGENGINE_REQUIRE_AUTH unset is the documented local-loopback opt-out;
    # the app must still boot, on the pre-existing warning only.
    monkeypatch.delenv("REGENGINE_REQUIRE_AUTH", raising=False)
    _clear_auth_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="inflow_lab")

    with TestClient(create_app()) as client:
        response = client.get("/api/healthz")
        assert response.status_code == 200

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    assert any("WITHOUT authentication" in message for message in messages)


@pytest.mark.parametrize("falsy_value", ["0", "false", "False", "no", "off", ""])
def test_require_auth_recognizes_explicit_false_spellings_as_opt_out(monkeypatch, falsy_value):
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", falsy_value)
    assert _shared_deployment_requires_auth() is False


@pytest.mark.parametrize("truthy_value", ["1", "true", "True", "yes", "on"])
def test_require_auth_recognizes_common_true_spellings(monkeypatch, truthy_value):
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", truthy_value)
    assert _shared_deployment_requires_auth() is True


def test_require_auth_defaults_off_when_unset(monkeypatch):
    monkeypatch.delenv("REGENGINE_REQUIRE_AUTH", raising=False)
    assert _shared_deployment_requires_auth() is False


def test_malformed_cors_origin_no_longer_crashes_create_app(monkeypatch):
    _clear_cors_env(monkeypatch)
    # One well-formed origin plus one missing its scheme -- the exact typo
    # reproduced in #178 -- must not raise out of create_app().
    monkeypatch.setenv(
        "REGENGINE_CORS_ORIGINS",
        "https://good.example.com, bad-domain-no-scheme",
    )

    app = create_app()  # must not raise

    with TestClient(app) as client:
        # The valid entry survives and is actually wired into the running
        # CORS middleware, not just returned by a bare function call.
        allowed = client.get("/api/healthz", headers={"Origin": "https://good.example.com"})
        assert allowed.headers["access-control-allow-origin"] == "https://good.example.com"

        # An origin that was never configured is still rejected -- the fix
        # skips the bad entry, it does not fall open to every origin.
        blocked = client.get("/api/healthz", headers={"Origin": "https://untrusted.example"})
        assert "access-control-allow-origin" not in blocked.headers


def test_resolve_cors_origins_skips_bad_entry_and_keeps_the_rest(monkeypatch, caplog):
    _clear_cors_env(monkeypatch)
    monkeypatch.setenv(
        "REGENGINE_CORS_ORIGINS",
        "https://good.example.com, bad-domain-no-scheme",
    )
    caplog.set_level(logging.WARNING, logger="inflow_lab.cors")

    assert resolve_cors_origins() == ["https://good.example.com"]

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab.cors"]
    assert any("bad-domain-no-scheme" in message for message in messages)


def test_resolve_cors_origins_falls_back_to_defaults_when_nothing_survives(monkeypatch):
    _clear_cors_env(monkeypatch)
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "bad-domain-no-scheme, also-bad")

    assert resolve_cors_origins() == list(DEFAULT_CORS_ORIGINS)


def test_cors_origins_from_env_still_raises_for_direct_callers(monkeypatch):
    # #178's fix is scoped to the create_app() path. Direct/test callers of
    # the strict function keep the existing raise -- test_api.py's
    # test_cors_origins_can_be_configured_without_wildcard_credentials
    # already depends on this contract for other malformed shapes.
    _clear_cors_env(monkeypatch)
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "bad-domain-no-scheme")

    with pytest.raises(ValueError, match="HTTP\\(S\\) origins"):
        cors_origins_from_env()


# ---------------------------------------------------------------------------
# #200 - the same malformed variable on the REQUEST path
# ---------------------------------------------------------------------------


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def test_malformed_cors_origin_does_not_500_state_changing_requests(monkeypatch):
    """#200: #178 fixed create_app(); this pins the request path.

    ``_reject_untrusted_unsafe_origin`` consults the trusted-origin list on
    every authenticated state-changing request. While it called the strict
    ``cors_origins_from_env()``, one malformed entry raised there, and
    ``auth_and_tenant_middleware``'s handler logs and re-raises -- so the app
    booted cleanly on a warning and then 500'd every mutating browser
    request. That is strictly worse than the pre-#178 hard crash, because it
    is silent at boot.

    This test would have caught it: the pre-fix code returns 500 for both
    origins below.
    """
    _clear_cors_env(monkeypatch)
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://good.example.com, bad-domain-no-scheme")
    # Auth on is what makes this branch reachable at all -- and the Dockerfile
    # now forces it on for every container deployment.
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-password")
    monkeypatch.delenv("REGENGINE_REQUIRE_AUTH", raising=False)

    auth = _basic_auth_header("demo-user", "demo-password")

    with TestClient(create_app()) as client:
        trusted = client.post(
            "/api/simulate/reset",
            json={},
            headers={"Authorization": auth, "Origin": "https://good.example.com"},
        )
        # The surviving good entry is honored, so the origin gate passes and
        # the request reaches the route. Any non-500 status proves the guard
        # itself did not blow up; it must specifically not be 403 either,
        # since this origin *was* configured.
        assert trusted.status_code != 500
        assert trusted.status_code != 403

        untrusted = client.post(
            "/api/simulate/reset",
            json={},
            headers={"Authorization": auth, "Origin": "https://untrusted.example"},
        )
        # Skipping the malformed entry must not fall open: an origin that was
        # never configured is still refused, and refused deliberately (403),
        # not by crashing (500).
        assert untrusted.status_code == 403


def test_resolve_cors_origins_cached_reparses_when_the_environment_changes(monkeypatch):
    """The request-path cache is keyed on the variables it derives from.

    A cache that ignored the environment would be a correctness bug, not just
    a stale read: an operator fixing a typo and restarting nothing would keep
    the old answer for the life of the process.
    """
    _clear_cors_env(monkeypatch)
    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://first.example.com")
    assert resolve_cors_origins_cached() == ["https://first.example.com"]

    monkeypatch.setenv("REGENGINE_CORS_ORIGINS", "https://second.example.com")
    assert resolve_cors_origins_cached() == ["https://second.example.com"]

    # And a repeat call at the same environment is still correct (the cache
    # hit path, which is the one that actually runs per request).
    assert resolve_cors_origins_cached() == ["https://second.example.com"]

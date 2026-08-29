from __future__ import annotations

import httpx
import pytest

from app.build_info import APP_VERSION
from scripts.remote_smoke import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_TENANT,
    FRESH_CUT_OUTPUT_LOT,
    RemoteSmokeConfig,
    RemoteSmokeFailure,
    config_from_env,
    run_remote_smoke,
)


def test_config_from_env_requires_connection_and_auth_values():
    with pytest.raises(RemoteSmokeFailure, match="REGENGINE_REMOTE_BASE_URL"):
        config_from_env({})

    config = config_from_env(
        {
            "REGENGINE_REMOTE_BASE_URL": "https://demo.example.com/",
            "REGENGINE_REMOTE_USERNAME": "demo",
            "REGENGINE_REMOTE_PASSWORD": "secret-password",
            # #124: config_from_env now refuses to build a credential-carrying
            # config for a host outside the allowlist. This test is about env
            # parsing, not host policy, so it declares its example host.
            "REGENGINE_REMOTE_ALLOWED_HOSTS": "demo.example.com",
        }
    )

    assert config.base_url == "https://demo.example.com"
    assert config.tenant == DEFAULT_TENANT
    assert config.allowed_origin == "https://demo.example.com"
    assert config.expected_build_sha is None

    expected_config = config_from_env(
        {
            "REGENGINE_REMOTE_BASE_URL": "https://demo.example.com/",
            "REGENGINE_REMOTE_USERNAME": "demo",
            "REGENGINE_REMOTE_PASSWORD": "secret-password",
            "REGENGINE_EXPECTED_BUILD_SHA": "abcdef1234567890",
            "REGENGINE_REMOTE_ALLOWED_HOSTS": "demo.example.com",
        }
    )
    assert expected_config.expected_build_sha == "abcdef1234567890"


def test_remote_smoke_success_uses_basic_auth_and_dedicated_tenant():
    server = FakeRemoteServer()
    config = RemoteSmokeConfig(
        base_url="https://demo.example.com",
        username="demo",
        password="secret-password",
    )

    with httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(server.handle),
    ) as client:
        summary = run_remote_smoke(config, client=client)

    assert summary == {
        "tenant": DEFAULT_TENANT,
        "fixture_stored": 13,
        "fixture_posted": 13,
        "lineage_records": 3,
        "epcis_events": 1,
        "build_version": APP_VERSION,
        "build_commit": "abcdef1",
    }

    healthz = server.requests[0]
    unauthenticated_health = server.requests[1]
    authenticated_requests = server.requests[2:]

    assert healthz.url.path == "/api/healthz"
    assert "authorization" not in healthz.headers
    assert unauthenticated_health.url.path == "/api/health"
    assert "authorization" not in unauthenticated_health.headers
    assert all("authorization" in request.headers for request in authenticated_requests)
    assert all(
        request.headers["x-regengine-tenant"] == DEFAULT_TENANT
        for request in authenticated_requests
    )
    assert server.fixture_request_json == {
        "reset": True,
        "source": "remote-smoke",
        "delivery": {"mode": "mock"},
    }


def test_remote_smoke_redacts_password_from_failure_messages():
    server = FakeRemoteServer(fail_reset=True)
    config = RemoteSmokeConfig(
        base_url="https://demo.example.com",
        username="demo",
        password="secret-password",
    )

    with httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(server.handle),
    ) as client:
        with pytest.raises(RemoteSmokeFailure) as exc_info:
            run_remote_smoke(config, client=client)

    failure_message = str(exc_info.value)
    assert "secret-password" not in failure_message
    assert "[redacted]" in failure_message


def test_remote_smoke_fails_when_expected_build_sha_does_not_match():
    server = FakeRemoteServer()
    config = RemoteSmokeConfig(
        base_url="https://demo.example.com",
        username="demo",
        password="secret-password",
        expected_build_sha="fedcba9876543210",
    )

    with httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(server.handle),
    ) as client:
        with pytest.raises(RemoteSmokeFailure, match="healthz build commit"):
            run_remote_smoke(config, client=client)


class FakeRemoteServer:
    def __init__(self, *, fail_reset: bool = False) -> None:
        self.fail_reset = fail_reset
        self.requests: list[httpx.Request] = []
        self.fixture_request_json: dict | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/healthz":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "build": {
                        "version": APP_VERSION,
                        "commit_sha": "abcdef1234567890",
                        "commit_sha_short": "abcdef1",
                    },
                },
            )
        if path == "/api/health":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"detail": "Not authenticated"})
            headers = self.cors_headers(request)
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "ok": True,
                    "tenant": request.headers["x-regengine-tenant"],
                    "auth": {
                        "enabled": True,
                        "username": "demo",
                        "uses_default_storage": False,
                    },
                    "status": {
                        "config": {
                            "persist_path": "data/tenants/remote-smoke/events.jsonl",
                            "delivery": {"mode": "mock"},
                        }
                    },
                },
            )
        if path == "/api/simulate/reset":
            if self.fail_reset:
                return httpx.Response(
                    500,
                    text="reset failed while handling secret-password",
                )
            return httpx.Response(200, json={"status": "reset"})
        if path == "/api/demo-fixtures/fresh_cut_transformation/load":
            self.fixture_request_json = decode_json(request)
            return httpx.Response(
                200,
                json={
                    "status": "loaded",
                    "fixture_id": "fresh_cut_transformation",
                    "scenario": "fresh_cut_processor",
                    "loaded": 13,
                    "stored": 13,
                    "posted": 13,
                    "failed": 0,
                    "source": "remote-smoke",
                    "delivery_mode": "mock",
                    "delivery_attempts": 1,
                    "lot_codes": [FRESH_CUT_OUTPUT_LOT],
                    "response": {},
                    "error": None,
                },
            )
        if path == f"/api/lineage/{FRESH_CUT_OUTPUT_LOT}":
            return httpx.Response(
                200,
                json={
                    "traceability_lot_code": FRESH_CUT_OUTPUT_LOT,
                    "records": [
                        {"event": {"traceability_lot_code": "TLC-DEMO-FC-HARVEST-001"}},
                        {"event": {"traceability_lot_code": "TLC-DEMO-FC-PACK-001"}},
                        {"event": {"traceability_lot_code": FRESH_CUT_OUTPUT_LOT}},
                    ],
                    "nodes": [],
                    "edges": [],
                },
            )
        if path == "/api/mock/regengine/export/fda-request":
            return httpx.Response(200, text="traceability_lot_code,batch\nTLC,BATCH-DEMO-FC-001\n")
        if path == "/api/mock/regengine/export/epcis":
            return httpx.Response(
                200,
                json={"epcisBody": {"eventList": [{"type": "TransformationEvent"}]}},
            )
        return httpx.Response(404, text=f"Unhandled path {path}")

    def cors_headers(self, request: httpx.Request) -> dict[str, str]:
        origin = request.headers.get("origin")
        if origin == "https://demo.example.com":
            return {
                "access-control-allow-origin": origin,
                "access-control-allow-credentials": "true",
            }
        return {}


def decode_json(request: httpx.Request) -> dict:
    body = request.content.decode("utf-8")
    return httpx.Response(200, content=body).json()


# ---------------------------------------------------------------------------
# #124 — the dispatcher-supplied base_url must not be able to walk off with
# the live shared-demo Basic Auth credentials.
# ---------------------------------------------------------------------------


def _credentialed_env(base_url: str) -> dict[str, str]:
    return {
        "REGENGINE_REMOTE_BASE_URL": base_url,
        "REGENGINE_REMOTE_USERNAME": "demo",
        "REGENGINE_REMOTE_PASSWORD": "secret-password",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "https://attacker.example",
        "http://attacker.example",
        # netloc contains the allowlisted host as userinfo, but every request
        # would go to attacker.example -- a netloc substring check would let
        # this through.
        "https://regengine-inflow-lab-gh-production.up.railway.app@attacker.example",
        # Allowlisted host as a subdomain label of an attacker domain.
        "https://regengine-inflow-lab-gh-production.up.railway.app.attacker.example",
        # Suffix-style near miss.
        "https://evil-regengine-inflow-lab-gh-production.up.railway.app",
    ],
    ids=["https_offlist", "http_offlist", "userinfo_prefix", "subdomain_prefix", "suffix_near_miss"],
)
def test_off_allowlist_base_url_is_refused_before_a_config_carries_secrets(base_url):
    with pytest.raises(RemoteSmokeFailure) as exc_info:
        config_from_env(_credentialed_env(base_url))

    message = str(exc_info.value)
    assert "Refusing to send credentials" in message
    # The failure message must not leak the password it was protecting.
    assert "secret-password" not in message


def test_the_real_demo_host_is_still_accepted():
    """Acceptance criterion: existing scheduled runs keep passing."""
    (demo_host,) = DEFAULT_ALLOWED_HOSTS
    config = config_from_env(_credentialed_env(f"https://{demo_host}/"))

    assert config.base_url == f"https://{demo_host}"


def test_plaintext_http_to_an_allowlisted_host_is_refused():
    """Basic Auth over http:// puts the shared-demo password on the wire in
    a base64 header for anyone on the path, so an allowlisted host is not on
    its own sufficient.
    """
    (demo_host,) = DEFAULT_ALLOWED_HOSTS
    with pytest.raises(RemoteSmokeFailure, match="plaintext HTTP"):
        config_from_env(_credentialed_env(f"http://{demo_host}"))


def test_allowlist_env_var_replaces_rather_than_extends_the_defaults():
    (demo_host,) = DEFAULT_ALLOWED_HOSTS
    env = _credentialed_env(f"https://{demo_host}") | {
        "REGENGINE_REMOTE_ALLOWED_HOSTS": "staging.example.com"
    }

    with pytest.raises(RemoteSmokeFailure, match="not an allowlisted"):
        config_from_env(env)

    allowed = config_from_env(
        _credentialed_env("https://staging.example.com")
        | {"REGENGINE_REMOTE_ALLOWED_HOSTS": "staging.example.com"}
    )
    assert allowed.base_url == "https://staging.example.com"


def test_loopback_is_always_permitted_for_local_runs():
    config = config_from_env(_credentialed_env("http://127.0.0.1:8000"))

    assert config.base_url == "http://127.0.0.1:8000"

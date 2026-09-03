from __future__ import annotations

import httpx
import pytest

from app.build_info import APP_VERSION
from scripts.remote_smoke import (
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

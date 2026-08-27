"""Security-hardening regressions.

Covers the egress restriction on live delivery (#87), the remote-smoke
base_url allowlist (#124), the fail-closed auth guard (#88), the
non-short-circuiting Basic Auth comparison (#89), and the capture of
RegEngine's response body on live ingest failures (#138).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import auth_required_from_env, enforce_auth_requirement, BasicAuthConfig
from app.main import app
from app.regengine_client import (
    BLOCKED_ENDPOINT_VERDICT,
    ALLOW_CLEARTEXT_DELIVERY_ENV,
    ALLOW_PRIVATE_DELIVERY_ENV,
    ALLOWED_DELIVERY_HOSTS_ENV,
    BlockedDeliveryEndpointError,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
    assert_delivery_endpoint_allowed,
)
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.integration import CREDENTIALS_WITHHELD_VERDICT
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig
from scripts.remote_smoke import (
    ALLOWED_HOSTS_ENV,
    RemoteSmokeFailure,
    config_from_env,
    normalize_base_url,
)


client = TestClient(app)


def make_payload() -> IngestPayload:
    return IngestPayload(
        source="security-tests",
        events=[
            RegEngineEvent(
                cte_type=CTEType.RECEIVING,
                traceability_lot_code="00012345678901-LOT-2026-001",
                product_description="Romaine Lettuce",
                quantity=10,
                unit_of_measure="cases",
                location_name="Distribution Center #4",
                timestamp=datetime(2026, 2, 5, 8, 30, tzinfo=UTC),
                kdes={
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                },
            )
        ],
    )


def make_live_config(endpoint: str) -> SimulationConfig:
    return SimulationConfig.model_validate(
        {
            "delivery": {
                "mode": "live",
                "endpoint": endpoint,
                "api_key": "rge_live_secret_key",
                "tenant_id": "11111111-1111-1111-1111-111111111111",
            }
        }
    )


class ExplodingAsyncClient:
    """Any outbound call at all is a test failure."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "ExplodingAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"blocked endpoint was contacted: {args} {kwargs}")

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"blocked endpoint was contacted: {args} {kwargs}")


# --- #87 egress restriction ------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/api/v1/webhooks/ingest",
        "http://localhost:8000/api/v1/webhooks/ingest",
        "http://10.0.0.5/api/v1/webhooks/ingest",
        "http://192.168.1.10/api/v1/webhooks/ingest",
        "http://[::1]/api/v1/webhooks/ingest",
        "http://metadata.google.internal/computeMetadata/v1/",
        "ftp://www.regengine.co/api/v1/webhooks/ingest",
    ],
)
def test_blocked_delivery_endpoints_are_refused(endpoint: str) -> None:
    with pytest.raises(BlockedDeliveryEndpointError):
        assert_delivery_endpoint_allowed(endpoint)


def test_public_delivery_endpoint_is_allowed() -> None:
    assert_delivery_endpoint_allowed("https://www.regengine.co/api/v1/webhooks/ingest")


def test_optional_strict_host_allowlist_rejects_other_hosts(monkeypatch: Any) -> None:
    monkeypatch.setenv(ALLOWED_DELIVERY_HOSTS_ENV, ".regengine.co")
    assert_delivery_endpoint_allowed("https://www.regengine.co/api/v1/webhooks/ingest")
    with pytest.raises(BlockedDeliveryEndpointError):
        assert_delivery_endpoint_allowed("https://attacker.example/ingest")


def test_local_stack_opt_out_allows_loopback(monkeypatch: Any) -> None:
    monkeypatch.setenv(ALLOW_PRIVATE_DELIVERY_ENV, "1")
    assert_delivery_endpoint_allowed("http://localhost:8000/api/v1/webhooks/ingest")


# --- cleartext delivery ----------------------------------------------------


def test_cleartext_delivery_to_a_public_host_is_refused() -> None:
    """The API key rides in an `Authorization` header; `http` sends it in clear."""
    with pytest.raises(BlockedDeliveryEndpointError) as excinfo:
        assert_delivery_endpoint_allowed("http://www.regengine.co/api/v1/webhooks/ingest")

    assert "cleartext" in str(excinfo.value)


def test_cleartext_opt_in_allows_a_public_http_endpoint(monkeypatch: Any) -> None:
    monkeypatch.setenv(ALLOW_CLEARTEXT_DELIVERY_ENV, "1")
    assert_delivery_endpoint_allowed("http://www.regengine.co/api/v1/webhooks/ingest")


def test_cleartext_opt_in_does_not_unblock_private_hosts(monkeypatch: Any) -> None:
    """It relaxes the scheme and nothing else."""
    monkeypatch.setenv(ALLOW_CLEARTEXT_DELIVERY_ENV, "1")
    with pytest.raises(BlockedDeliveryEndpointError):
        assert_delivery_endpoint_allowed("http://169.254.169.254/latest/meta-data/")


def test_local_stack_opt_out_still_permits_cleartext(monkeypatch: Any) -> None:
    """`REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS` already implies a trusted network.

    The customer-journey harness points at `http://localhost:8000`; requiring a
    second flag for that would break the documented local workflow.
    """
    monkeypatch.setenv(ALLOW_PRIVATE_DELIVERY_ENV, "1")
    assert_delivery_endpoint_allowed("http://localhost:8000/api/v1/webhooks/ingest")


def test_a_local_endpoint_is_reported_as_local_not_as_cleartext() -> None:
    """The finding an operator must act on is the destination, not the scheme."""
    with pytest.raises(BlockedDeliveryEndpointError) as excinfo:
        assert_delivery_endpoint_allowed("http://169.254.169.254/latest/meta-data/")

    assert "not an allowed destination" in str(excinfo.value)


def test_live_ingest_refuses_metadata_endpoint_without_sending(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ExplodingAsyncClient)
    config = make_live_config("http://169.254.169.254/latest/meta-data/")

    with pytest.raises(BlockedDeliveryEndpointError):
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))


def test_check_connection_refuses_metadata_endpoint_without_sending(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ExplodingAsyncClient)
    config = make_live_config("http://169.254.169.254/latest/meta-data/")

    result = asyncio.run(LiveRegEngineClient().check_connection(config))

    assert result.verdict == BLOCKED_ENDPOINT_VERDICT
    assert "not an allowed destination" in result.detail


def test_integration_test_never_sends_stored_credentials_to_another_host(
    monkeypatch: Any,
) -> None:
    """A caller-supplied endpoint must not inherit the saved API key."""
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ExplodingAsyncClient)
    configure = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "api_key": "rge_live_stored_secret",
            "tenant_id": "22222222-2222-2222-2222-222222222222",
        },
    )
    assert configure.status_code == 200
    try:
        response = client.post(
            "/api/integration/test",
            json={"endpoint": "https://attacker.example/api/v1/webhooks/ingest"},
        )
        assert response.status_code == 200
        body = response.json()
        # No credentials were inherited, so the probe stops before any request
        # (ExplodingAsyncClient would raise if one were attempted). The verdict
        # names the real reason: credentials exist, they were withheld.
        assert body["verdict"] == CREDENTIALS_WITHHELD_VERDICT
        assert "never sent anywhere else" in body["detail"]
        assert "rge_live_stored_secret" not in response.text

        blocked = client.post(
            "/api/integration/test",
            json={
                "endpoint": "http://169.254.169.254/latest/meta-data/",
                "api_key": "rge_live_probe_key",
                "tenant_id": "22222222-2222-2222-2222-222222222222",
            },
        )
        assert blocked.status_code == 200
        assert blocked.json()["verdict"] == BLOCKED_ENDPOINT_VERDICT
    finally:
        client.post("/api/integration/configure", json={"mode": "mock"})


# --- #138 live ingest error bodies ----------------------------------------


def _failing_client_factory(status_code: int, body: bytes, content_type: str):
    class FailingAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FailingAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, endpoint: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                status_code,
                content=body,
                headers={"content-type": content_type},
                request=httpx.Request("POST", endpoint),
            )

    return FailingAsyncClient


def test_live_ingest_failure_keeps_regengine_json_detail(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient",
        _failing_client_factory(
            402, b'{"detail":"Subscription is past_due"}', "application/json"
        ),
    )
    config = make_live_config("https://www.regengine.co/api/v1/webhooks/ingest")

    with pytest.raises(LiveRegEngineDeliveryError) as excinfo:
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert "Subscription is past_due" in str(excinfo.value)
    assert excinfo.value.metadata["status_code"] == 402
    assert excinfo.value.metadata["error_body"] == "Subscription is past_due"


def test_live_ingest_failure_degrades_to_raw_text_body(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient",
        _failing_client_factory(429, b"<html>slow down</html>", "text/html"),
    )
    config = make_live_config("https://www.regengine.co/api/v1/webhooks/ingest")

    with pytest.raises(LiveRegEngineDeliveryError) as excinfo:
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), config))

    assert "slow down" in str(excinfo.value)
    assert excinfo.value.metadata["status_code"] == 429


# --- #124 remote smoke allowlist ------------------------------------------


def test_remote_smoke_rejects_off_allowlist_base_url() -> None:
    with pytest.raises(RemoteSmokeFailure, match="is not allowed"):
        config_from_env(
            {
                "REGENGINE_REMOTE_BASE_URL": "https://attacker.example",
                "REGENGINE_REMOTE_USERNAME": "demo",
                "REGENGINE_REMOTE_PASSWORD": "secret-password",
            }
        )


def test_remote_smoke_allows_the_shared_demo_host() -> None:
    base = "https://regengine-inflow-lab-gh-production.up.railway.app"
    config = config_from_env(
        {
            "REGENGINE_REMOTE_BASE_URL": base + "/",
            "REGENGINE_REMOTE_USERNAME": "demo",
            "REGENGINE_REMOTE_PASSWORD": "secret-password",
        }
    )
    assert config.base_url == base


def test_remote_smoke_allowlist_is_extendable_by_env_not_dispatch_input() -> None:
    config = config_from_env(
        {
            "REGENGINE_REMOTE_BASE_URL": "https://staging.internal.example",
            "REGENGINE_REMOTE_USERNAME": "demo",
            "REGENGINE_REMOTE_PASSWORD": "secret-password",
            ALLOWED_HOSTS_ENV: "staging.internal.example",
        }
    )
    assert config.base_url == "https://staging.internal.example"

    with pytest.raises(RemoteSmokeFailure):
        normalize_base_url("https://evil.example", allowed_hosts=("demo.example.com",))


# --- #88 fail-closed auth guard -------------------------------------------


def test_startup_guard_fails_closed_without_basic_auth(monkeypatch: Any) -> None:
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", "1")
    monkeypatch.delenv("REGENGINE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("REGENGINE_BASIC_AUTH_PASSWORD", raising=False)

    assert auth_required_from_env() is True
    with pytest.raises(RuntimeError, match="REGENGINE_REQUIRE_AUTH"):
        enforce_auth_requirement()


def test_startup_guard_passes_when_auth_is_configured(monkeypatch: Any) -> None:
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", "1")
    enforce_auth_requirement(BasicAuthConfig(username="demo", password="secret"))


def test_startup_guard_has_explicit_loopback_opt_out(monkeypatch: Any) -> None:
    monkeypatch.setenv("REGENGINE_REQUIRE_AUTH", "0")
    monkeypatch.delenv("REGENGINE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("REGENGINE_BASIC_AUTH_PASSWORD", raising=False)

    assert auth_required_from_env() is False
    enforce_auth_requirement()


def test_startup_guard_is_inert_when_signal_is_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("REGENGINE_REQUIRE_AUTH", raising=False)
    enforce_auth_requirement(BasicAuthConfig(username=None, password=None))


# --- #89 non-short-circuiting Basic Auth comparison ------------------------


def test_basic_auth_runs_both_comparisons(monkeypatch: Any) -> None:
    """Both compare_digest calls must run, whatever the username is."""
    import app.auth as auth_module

    calls: list[tuple[str, str]] = []
    real_compare = auth_module.secrets.compare_digest

    def counting_compare(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_module.secrets, "compare_digest", counting_compare)
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "secret-password")

    import base64

    for username in ("demo", "wrong-user"):
        calls.clear()
        token = base64.b64encode(f"{username}:bad-password".encode()).decode()
        response = client.get("/api/health", headers={"Authorization": f"Basic {token}"})
        assert response.status_code == 401
        # Two comparisons regardless of whether the username matched.
        assert len(calls) == 2, (username, calls)

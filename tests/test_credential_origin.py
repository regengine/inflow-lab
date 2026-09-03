"""The stored RegEngine credentials belong to one *origin*, not one host.

`POST /api/integration/test` accepts a caller-supplied endpoint and probes it
with the operator's saved API key and tenant id. Deciding whether to hand those
credentials over used to compare only `urlparse().hostname`, which discards the
scheme, the port and any userinfo. Three variants of the configured host
therefore inherited the key:

* `http://` instead of `https://` -- the probe, and the
  `X-RegEngine-API-Key` header on it, then left in cleartext.
* a different port -- `https://host:8443` reaches whatever else listens there.
* embedded userinfo -- the caller's own credentials ride along.

Each is a different security context than the one the operator configured, so
each must be treated exactly the way a different host already was: the stored
credentials are withheld and nothing is sent. These tests pin that, plus the
normalisations that must NOT change the origin (case, trailing dot, an explicit
default port) and the happy path where an exact match still inherits.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.regengine_client as regengine_client
from app.main import app, controller
from app.routers.integration import _endpoint_origin, _same_origin
from app.schemas.integration import CREDENTIALS_WITHHELD_VERDICT
from app.schemas.simulation import SimulationConfig


client = TestClient(app)

CONFIGURED_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"
STORED_API_KEY = "rge_live_stored_origin_secret"
STORED_TENANT = "33333333-3333-3333-3333-333333333333"


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


class ExplodingAsyncClient:
    """Any outbound request at all is a test failure."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "ExplodingAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"outbound request must not happen: {args} {kwargs}")

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"outbound request must not happen: {args} {kwargs}")


class _Response:
    status_code = 200

    def json(self) -> Any:
        raise ValueError("not json")  # /health posture stays unknown


class RecordingAsyncClient:
    """Records every outbound call so headers can be inspected."""

    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "RecordingAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _Response:
        RecordingAsyncClient.calls.append({"url": str(url), "headers": dict(headers or {})})
        return _Response()


def configure_live_credentials() -> None:
    response = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": CONFIGURED_ENDPOINT,
            "api_key": STORED_API_KEY,
            "tenant_id": STORED_TENANT,
        },
    )
    assert response.status_code == 200
    status = response.json()
    assert status["api_key_configured"] and status["tenant_configured"]


# --- origin normalisation (unit) ------------------------------------------


def test_origin_makes_the_default_port_explicit() -> None:
    assert _endpoint_origin("https://host/x") == ("https", "host", 443)
    assert _endpoint_origin("https://host:443/x") == ("https", "host", 443)
    assert _endpoint_origin("http://host/x") == ("http", "host", 80)
    assert _endpoint_origin("http://host:80/x") == ("http", "host", 80)
    assert _endpoint_origin("https://HOST./x") == ("https", "host", 443)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:pw@host/x",  # userinfo is not part of the saved endpoint
        "https://host:notaport/x",  # malformed port
        "https://host:99999/x",  # out-of-range port
        "//host/x",  # no scheme
        "https:///x",  # no host
    ],
)
def test_unusable_origin_never_matches_anything(endpoint: str) -> None:
    assert _endpoint_origin(endpoint) is None
    assert not _same_origin(endpoint, CONFIGURED_ENDPOINT)
    # Not even against itself: None is "no origin", not "some origin".
    assert not _same_origin(endpoint, endpoint)


def test_same_origin_ignores_path_case_and_trailing_dot() -> None:
    assert _same_origin("https://WWW.Regengine.CO./other/path", CONFIGURED_ENDPOINT)
    assert _same_origin("https://www.regengine.co:443/", CONFIGURED_ENDPOINT)


# --- the router withholds stored credentials off-origin -------------------


@pytest.mark.parametrize(
    ("label", "endpoint"),
    [
        ("scheme downgrade", "http://www.regengine.co/api/v1/webhooks/ingest"),
        ("port change", "https://www.regengine.co:8443/api/v1/webhooks/ingest"),
        ("userinfo", "https://evil%40attacker.example@www.regengine.co/api/v1/webhooks/ingest"),
        ("different host", "https://attacker.example/api/v1/webhooks/ingest"),
    ],
)
def test_stored_credentials_never_leave_the_configured_origin(
    monkeypatch: Any, label: str, endpoint: str
) -> None:
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient", ExplodingAsyncClient
    )
    configure_live_credentials()

    response = client.post("/api/integration/test", json={"endpoint": endpoint})

    assert response.status_code == 200, label
    body = response.json()
    # Nothing was sent (ExplodingAsyncClient would have raised) and nothing
    # about the stored credentials came back either.
    assert STORED_API_KEY not in response.text, label
    assert STORED_TENANT not in response.text, label
    assert body["verdict"] == CREDENTIALS_WITHHELD_VERDICT, label


def test_withheld_verdict_explains_what_actually_failed() -> None:
    """The old `not_configured` named a condition that had not failed.

    The credentials ARE configured; they were withheld on purpose. An operator
    told "configure credentials" re-enters the same correct key and gets the
    same result, so the message has to name the real reason.
    """
    configure_live_credentials()

    body = client.post(
        "/api/integration/test",
        json={"endpoint": "http://www.regengine.co/api/v1/webhooks/ingest"},
    ).json()

    assert body["verdict"] != "not_configured"
    detail = body["detail"]
    assert "https://www.regengine.co" in detail  # where the stored key belongs
    assert "http://www.regengine.co" in detail  # what was probed
    assert "scheme" in detail and "port" in detail
    assert body["endpoint_host"] == "www.regengine.co"


def test_not_configured_still_means_nothing_is_configured(monkeypatch: Any) -> None:
    """The generic verdict is kept for the case it truthfully describes."""
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient", ExplodingAsyncClient
    )
    body = client.post(
        "/api/integration/test",
        json={"endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest"},
    ).json()

    assert body["verdict"] == "not_configured"


def test_explicit_credentials_still_probe_a_different_origin(monkeypatch: Any) -> None:
    """Withholding is about the *stored* key, not about refusing the probe."""
    RecordingAsyncClient.calls = []
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient", RecordingAsyncClient
    )
    configure_live_credentials()

    body = client.post(
        "/api/integration/test",
        json={
            "endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest",
            "api_key": "rge_live_probe_key",
            "tenant_id": STORED_TENANT,
        },
    ).json()

    assert body["verdict"] == "connected"
    probe = RecordingAsyncClient.calls[0]
    assert probe["url"].startswith("https://staging.regengine.example/")
    assert probe["headers"]["X-RegEngine-API-Key"] == "rge_live_probe_key"
    assert STORED_API_KEY not in str(RecordingAsyncClient.calls)


# --- happy path: an exact origin match still inherits ---------------------


@pytest.mark.parametrize(
    ("label", "endpoint", "expected_scheme_host"),
    [
        ("exact", CONFIGURED_ENDPOINT, "https://www.regengine.co"),
        ("uppercase host", "https://WWW.REGENGINE.CO/api/v1/webhooks/ingest", "https://www.regengine.co"),
        ("trailing dot", "https://www.regengine.co./api/v1/webhooks/ingest", "https://www.regengine.co."),
        ("explicit :443", "https://www.regengine.co:443/api/v1/webhooks/ingest", "https://www.regengine.co"),
    ],
)
def test_exact_origin_match_still_inherits_stored_credentials(
    monkeypatch: Any, label: str, endpoint: str, expected_scheme_host: str
) -> None:
    RecordingAsyncClient.calls = []
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient", RecordingAsyncClient
    )
    configure_live_credentials()

    response = client.post("/api/integration/test", json={"endpoint": endpoint})

    assert response.json()["verdict"] == "connected", label
    probe = RecordingAsyncClient.calls[0]
    assert probe["url"] == f"{expected_scheme_host}/api/v1/webhooks/recent", label
    assert probe["headers"]["X-RegEngine-API-Key"] == STORED_API_KEY, label
    assert probe["headers"]["X-Tenant-ID"] == STORED_TENANT, label
    # ... and the response body still never echoes them.
    assert STORED_API_KEY not in response.text, label


def test_configured_endpoint_with_no_override_is_unaffected(monkeypatch: Any) -> None:
    RecordingAsyncClient.calls = []
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient", RecordingAsyncClient
    )
    configure_live_credentials()

    assert client.post("/api/integration/test").json()["verdict"] == "connected"
    assert RecordingAsyncClient.calls[0]["headers"]["X-RegEngine-API-Key"] == STORED_API_KEY


def test_default_endpoint_is_compared_by_origin_too(monkeypatch: Any) -> None:
    """No endpoint configured -> the client default is the credential origin."""
    monkeypatch.setattr(
        "app.regengine_client.httpx.AsyncClient", ExplodingAsyncClient
    )
    configure = client.post(
        "/api/integration/configure",
        json={"mode": "live", "api_key": STORED_API_KEY, "tenant_id": STORED_TENANT},
    )
    assert configure.status_code == 200
    default_origin = regengine_client.DEFAULT_LIVE_INGEST_ENDPOINT
    downgraded = default_origin.replace("https://", "http://", 1)

    body = client.post("/api/integration/test", json={"endpoint": downgraded}).json()

    assert body["verdict"] == CREDENTIALS_WITHHELD_VERDICT

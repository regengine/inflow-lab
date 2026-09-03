"""Outbound delivery must not reach internal hosts or lend out the stored key.

Three defects, all reachable without authentication on a default deployment
(`BasicAuthConfig.enabled` is false while the Basic Auth env vars are unset,
and the integration routes carry no auth dependency of their own):

* **Any endpoint was dialed, with the API key attached.** Nothing validated
  `DeliveryConfig.endpoint`; `_validate_live_delivery` checks only that the key
  and tenant are non-empty. Setting the endpoint to `169.254.169.254` reached
  cloud metadata *and* handed it `X-RegEngine-API-Key`. This is the substance
  of #207 -- though not its framing: #207 argues about a DNS-rebinding bypass
  of an egress guard, and there was no guard here to bypass.

* **The stored key was lent to a caller-supplied endpoint.** `/api/integration/
  test` overrides only the fields the request supplies, so a body carrying just
  an `endpoint` probed that host with the credential already on file.

* **Non-ASCII credentials crashed the comparison.** `secrets.compare_digest`
  on `str` raises TypeError unless both sides are ASCII, turning a 401 into an
  unauthenticated HTTP 500.

What the egress guard does NOT do is resolve hostnames: that would be blocking
I/O inside a validator, i.e. on the event loop, which is the defect #216 exists
for. A name resolving to a private address still passes, and pinning the
resolved address at connect time remains open.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.simulation import ALLOW_PRIVATE_ENDPOINTS_ENV, DeliveryConfig

client = TestClient(app)

BLOCKED = [
    "https://169.254.169.254/latest/meta-data/",   # cloud metadata
    "https://127.0.0.1:6379/",                      # loopback service
    "https://10.0.0.5/api",                         # RFC1918
    "https://192.168.1.1/api",
    "https://172.16.0.1/api",
    "https://[::1]/api",                            # IPv6 loopback
    "https://0.0.0.0/api",                          # unspecified
    "https://localhost:8001/api",                   # loopback by name
    "https://foo.localhost/api",
    "http://www.regengine.co/api",                  # cleartext credential
]

ALLOWED = [
    "https://www.regengine.co/api/v1/webhooks/ingest",
    "https://staging.regengine.example/api/v1/webhooks/ingest",
    "https://example.test/api/v1/webhooks/ingest",
]


@pytest.mark.parametrize("endpoint", BLOCKED)
def test_internal_and_cleartext_endpoints_are_rejected(endpoint):
    with pytest.raises(ValidationError):
        DeliveryConfig(endpoint=endpoint)


@pytest.mark.parametrize("endpoint", ALLOWED)
def test_ordinary_endpoints_are_accepted(endpoint):
    assert DeliveryConfig(endpoint=endpoint).endpoint is not None


def test_the_escape_hatch_opens_for_local_development(monkeypatch):
    monkeypatch.setenv(ALLOW_PRIVATE_ENDPOINTS_ENV, "1")

    assert DeliveryConfig(endpoint="http://localhost:8001/api").endpoint is not None


def test_the_guard_reaches_the_configure_route():
    response = client.post(
        "/api/integration/configure",
        json={"mode": "live", "endpoint": "https://169.254.169.254/x", "api_key": "k", "tenant_id": "t"},
    )

    assert response.status_code == 422, response.text


def test_a_rejection_says_which_lever_reopens_it():
    # An operator pointing at a local RegEngine must be told how, or they will
    # reasonably conclude the feature is broken.
    with pytest.raises(ValidationError) as excinfo:
        DeliveryConfig(endpoint="https://localhost:8001/api")

    assert ALLOW_PRIVATE_ENDPOINTS_ENV in str(excinfo.value)


# ---------------------------------------------------------------------------
# The stored API key is not lent to another origin
# ---------------------------------------------------------------------------


def _configure_live() -> None:
    response = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
            "api_key": "rge_live_STORED_SECRET",
            "tenant_id": "acme",
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example.net/api/v1/webhooks/ingest",   # different host
        "https://www.regengine.co:8443/api/v1/webhooks/ingest",  # different port
    ],
)
def test_probing_another_origin_without_a_key_is_refused(endpoint):
    _configure_live()

    response = client.post("/api/integration/test", json={"endpoint": endpoint})

    assert response.status_code == 400, response.text
    assert "api_key" in response.text


def test_probing_the_configured_origin_still_uses_the_stored_key():
    # The guard must not have broken the ordinary "test my saved config" flow.
    _configure_live()

    response = client.post(
        "/api/integration/test",
        json={"endpoint": "https://www.regengine.co/api/v1/webhooks/ingest"},
    )

    assert response.status_code == 200, response.text


def test_a_caller_supplying_its_own_key_may_probe_anywhere_allowed():
    _configure_live()

    response = client.post(
        "/api/integration/test",
        json={"endpoint": "https://staging.regengine.example/api/v1/webhooks/ingest", "api_key": "own-key"},
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Basic Auth credential comparison
# ---------------------------------------------------------------------------


def _get_status(username: str, password: str, monkeypatch) -> int:
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "operator")
    monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "correct-horse")
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    with TestClient(app, raise_server_exceptions=False) as authed:
        return authed.get(
            "/api/simulate/status", headers={"Authorization": f"Basic {token}"}
        ).status_code


@pytest.mark.parametrize(
    ("username", "password"),
    [("operätor", "correct-horse"), ("operator", "wröng"), ("ünïcode", "ünïcode")],
)
def test_non_ascii_credentials_are_rejected_not_crashed(username, password, monkeypatch):
    assert _get_status(username, password, monkeypatch) == 401


def test_correct_credentials_still_authenticate(monkeypatch):
    assert _get_status("operator", "correct-horse", monkeypatch) == 200


def test_wrong_ascii_credentials_still_401(monkeypatch):
    assert _get_status("operator", "nope", monkeypatch) == 401
    assert _get_status("nobody", "correct-horse", monkeypatch) == 401


# ---------------------------------------------------------------------------
# Non-finite quantities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_quantity_is_rejected(raw):
    # These parse as floats and `nan <= 0` is False, so they used to sail
    # through and land on disk as a bare NaN/Infinity token -- invalid JSON.
    from app.csv_importer import parse_csv_import

    csv_text = (
        "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,timestamp\n"
        f"harvesting,TLC-NF-1,Romaine,{raw},cases,Valley Fresh Farms,2026-02-10T08:00:00Z\n"
    )
    parsed = parse_csv_import("scheduled_events", csv_text)

    assert parsed.errors, f"{raw!r} was accepted as a quantity"
    assert any(error.field == "quantity" for error in parsed.errors)


def test_ordinary_quantities_still_import():
    from app.csv_importer import parse_csv_import

    csv_text = (
        "cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,"
        "location_name,timestamp\n"
        "harvesting,TLC-OK-1,Romaine,10.5,cases,Valley Fresh Farms,2026-02-10T08:00:00Z\n"
    )
    parsed = parse_csv_import("scheduled_events", csv_text)

    assert not parsed.errors
    assert parsed.events[0].quantity == 10.5

"""Tests for #100: the Test Connection probe must not report unqualified
success for credentials whose every ingest would fail.

------------------------------------------------------------------------
Why this file exists, and what it deliberately does NOT do
------------------------------------------------------------------------
check_connection() probes GET /api/v1/webhooks/recent, not POST
/api/v1/webhooks/ingest -- #100 itself is explicit that a probe which
actually exercised ingest would have to write a real event, and posting
a synthetic event into a customer's production RegEngine is not an
acceptable thing for a button labelled "Test connection" to do. So a
probe that returns HTTP 200 can only ever prove the credentials
authenticate for a READ; it can never prove the write path's three extra
gates (the tenant's subscription status, the separate webhooks.ingest
permission scope, and -- when configured -- the HMAC request signature)
will also pass. A read-only-scoped key and a fully-privileged one are
genuinely, correctly both HTTP 200 on /recent -- the probe cannot tell
them apart.

Given that ceiling, three fixes were available:

  1. A verdict-level fix: introduce e.g. verdict="read_only" or
     "ingest_unverified" for the HTTP-200 case instead of "connected".
     Rejected: tests/test_integration_settings.py, tests/test_egress_guard.py
     and tests/test_client_diagnostics.py all pin verdict == "connected"
     for a plain HTTP-200 probe response (scripts/customer_journey.py's
     live smoke check asserts the same thing outside pytest). None of
     those files are this change's to edit, and renaming the verdict
     would silently break all of them.
  2. A RegEngine-side fix: a dedicated preflight endpoint carrying
     /ingest's own gates without writing, or an HMAC-posture field on
     /health this client could diff against REGENGINE_WEBHOOK_HMAC_SECRET
     the way the contract-version read already diffs contract versions.
     Rejected at the time: app/mock_service.py and app/contract.py -- the
     only evidence this repo had for what RegEngine's contract actually
     offers -- showed no such endpoint or field, and building against one
     anyway would mean inventing contract surface with no confirmed
     evidence it exists, which is the same failure mode #100 is about
     (trusting an assumption nobody verified).

     Since then RegEngine's /health has grown exactly that field --
     `webhook_hmac_configured`, sourced from its webhook router's own
     posture check -- so the seam named here was wired through (#217):
     check_connection now reads the whole /health document once and, when
     the posture is advertised and disagrees with the local secret, answers
     "signature_mismatch" instead of "connected". The subscription gate
     and the webhooks.ingest scope remain unobservable from a read, so the
     tests below about the 200 detail text still stand. The absence of the
     field must never produce a verdict; that tolerance is pinned below.
  3. A claim-level fix: keep the verdict vocabulary exactly as pinned,
     and instead correct what the HTTP-200 case's *detail* text asserts
     -- say plainly that a read succeeded and name the three gates that
     were not checked, rather than implying the credential is safe to
     ingest with. This is the one implemented, in
     app/regengine_client.py's _CONNECTION_VERDICTS[200] entry.

So below: verdict == "connected" for a plain HTTP-200 probe is asserted
explicitly and is NOT a bug -- it is the pinned, load-bearing behavior
every other consumer of this client depends on. What changed, and what
most of these tests check, is that the detail string riding along with
it stops overclaiming what a read probe proved.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.regengine_client import (
    _CONNECTION_VERDICTS,
    _MAX_ERROR_BODY_CHARS,
    _extract_error_body,
    LiveRegEngineClient,
)
from app.schemas.simulation import DeliveryConfig, EgressBlockedError, SimulationConfig
from app.store import MASKED_SECRET


client = TestClient(app)


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def _live_config(
    *,
    api_key: str = "probe-api-key",
    tenant_id: str = "probe-tenant-id",
    endpoint: str | None = None,
) -> SimulationConfig:
    return SimulationConfig(
        delivery=DeliveryConfig(mode="live", api_key=api_key, tenant_id=tenant_id, endpoint=endpoint)
    )


class _FakeResponse:
    """Duck-typed httpx.Response stand-in. check_connection only ever
    touches .status_code on the probe response itself, and .json() on
    the separate /health response (for the contract-version handshake)."""

    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("response body is not JSON")
        return self._payload


def _fake_probe_client(*, status_code: int, health_payload: Any = None) -> type:
    """Build a fresh fake httpx.AsyncClient class for one test.

    A factory rather than one shared class with mutable class attributes
    (the pattern the rest of this suite's fakes use) so that tests here
    can never leak a `calls` list or status code into each other via
    shared class state.
    """

    class _FakeAsyncClient:
        calls: list[dict[str, Any]] = []

        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, Any] | None = None,
            # extensions: the live client attaches {"sni_hostname": ...} when it
            # pins the validated address for the dial (#207). Accepted and
            # ignored here so this fake keeps working on either path.
            extensions: dict[str, Any] | None = None,
        ) -> _FakeResponse:
            if url.endswith("/health"):
                return _FakeResponse(200, health_payload)
            _FakeAsyncClient.calls.append({"url": url, "headers": headers, "params": params})
            return _FakeResponse(status_code, None)

    return _FakeAsyncClient


class _UnreachableAsyncClient:
    """A fake httpx.AsyncClient that fails the test if it is ever
    constructed at all -- for proving the egress guard stops
    check_connection before any client is built, let alone dialed."""

    def __init__(self, *, timeout: float) -> None:  # pragma: no cover - must not run
        raise AssertionError(
            "httpx.AsyncClient must not be constructed once the egress guard has refused the host"
        )


# ---------------------------------------------------------------------------
# The core #100 fix: a passing read probe no longer claims ingest is safe
# ---------------------------------------------------------------------------


def test_connected_verdict_is_unchanged_for_a_plain_200_probe(monkeypatch: Any) -> None:
    """Documents the deliberate constraint this fix works within: renaming
    the verdict away from "connected" would break tests this change may
    not edit. See this file's module docstring, option 1."""
    fake = _fake_probe_client(status_code=200)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)

    result = asyncio.run(LiveRegEngineClient().check_connection(_live_config()))

    assert result.verdict == "connected"
    assert result.status_code == 200


def test_connected_detail_names_the_three_gates_a_read_probe_cannot_see(monkeypatch: Any) -> None:
    """The actual #100 fix. A green read probe must no longer read as "you
    are safe to ingest" -- it must name, in plain language, the three
    ingest-only gates (subscription, the webhooks.ingest permission
    scope, and the HMAC signature) that a GET to /recent cannot see."""
    fake = _fake_probe_client(status_code=200)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)

    result = asyncio.run(LiveRegEngineClient().check_connection(_live_config()))

    detail = result.detail.lower()
    assert "does not confirm" in detail, result.detail
    assert "subscription" in detail, result.detail
    assert "webhooks.ingest" in detail, result.detail
    assert "hmac" in detail or "signature" in detail, result.detail
    # The old, overclaiming wording must actually be gone, not merely
    # supplemented -- otherwise an operator skimming the first sentence
    # still walks away with the false assurance #100 is about.
    assert "credentials and tenant are valid" not in detail, result.detail


def test_read_only_scoped_key_still_reports_connected_but_detail_warns_of_the_gap(
    monkeypatch: Any,
) -> None:
    """The exact scenario #100 was filed about: a key with read scope but
    not the separate webhooks.ingest permission gets HTTP 200 from
    /recent (this probe) and would get HTTP 403 from an actual ingest.
    The probe cannot tell this key apart from a fully-privileged one --
    both are genuinely, correctly HTTP 200 on /recent -- so it cannot
    pick a different verdict for this case. #100's fix is that the
    detail text already told the operator this exact gap could exist,
    before they ever click ingest and get surprised by a 403.

    This stands in for the GitHub issue's own acceptance criterion ("a
    test in tests/test_integration_settings.py asserts the verdict for
    at least one config that passes /recent but fails /ingest") -- that
    file is not this change's to edit, so the equivalent case lives here
    instead.
    """
    fake = _fake_probe_client(status_code=200)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)
    read_only_key_config = _live_config(api_key="rge_live_read_only_scope_key")

    result = asyncio.run(LiveRegEngineClient().check_connection(read_only_key_config))

    assert result.verdict == "connected"
    assert "webhooks.ingest" in result.detail
    assert "403" in result.detail


@pytest.mark.parametrize(
    "status_code,expected_verdict",
    [
        (401, "unauthorized"),
        (402, "subscription_inactive"),
        (403, "forbidden"),
        (404, "tenant_mismatch"),
        (429, "rate_limited"),
        (503, "service_unavailable"),
    ],
)
def test_non_200_verdicts_are_untouched_by_the_100_fix(
    monkeypatch: Any, status_code: int, expected_verdict: str
) -> None:
    """#100 only rewrites the HTTP-200 detail text. Every other status
    code's verdict, and its own detail wording, is unchanged -- pinned
    here directly against the dict, independent of the router-level pins
    in tests/test_integration_settings.py and tests/test_client_diagnostics.py."""
    fake = _fake_probe_client(status_code=status_code)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)

    result = asyncio.run(LiveRegEngineClient().check_connection(_live_config()))

    assert result.verdict == expected_verdict
    assert result.status_code == status_code
    # Untouched by #100: still the exact same detail text this dict
    # carried before this change.
    assert result.detail == _CONNECTION_VERDICTS[status_code][1]


def test_contract_mismatch_still_overrides_the_reworded_connected_detail(monkeypatch: Any) -> None:
    """The contract-version-skew upgrade (verdict="contract_mismatch") sits
    on top of a 200 probe response and fully replaces both verdict and
    detail. Confirms the #100 rewrite of the 200 entry didn't disturb
    that override's own wiring, since it only reads
    _CONNECTION_VERDICTS[200] to decide whether the base verdict was
    "connected" before swapping in the mismatch-specific text."""
    from app.contract import INFLOW_CONTRACT_VERSION

    fake = _fake_probe_client(
        status_code=200,
        health_payload={"inflow_contract_version": f"{INFLOW_CONTRACT_VERSION}-stale-test"},
    )
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)

    result = asyncio.run(LiveRegEngineClient().check_connection(_live_config()))

    assert result.verdict == "contract_mismatch"
    assert "ingest contract" in result.detail
    assert "does not confirm" not in result.detail.lower()  # the 200 wording was fully replaced


# ---------------------------------------------------------------------------
# #217: the one ingest-only gate that IS observable without writing.
# RegEngine's /health advertises `webhook_hmac_configured`; a 200 probe
# whose local signing posture disagrees with it is a signature_mismatch.
# ---------------------------------------------------------------------------


def _probe_with_health(monkeypatch: Any, health_payload: Any, *, local_secret: str | None) -> Any:
    from app.regengine_client import WEBHOOK_HMAC_SECRET_ENV

    if local_secret is None:
        monkeypatch.delenv(WEBHOOK_HMAC_SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, local_secret)
    fake = _fake_probe_client(status_code=200, health_payload=health_payload)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)
    return asyncio.run(LiveRegEngineClient().check_connection(_live_config()))


def test_remote_signing_without_a_local_secret_is_a_signature_mismatch(monkeypatch: Any) -> None:
    """RegEngine verifies signatures, this side sends none: every live
    ingest would 401 while the read probe stays green. That is the
    half-configured demo #217 is about, and it must not read "connected"."""
    result = _probe_with_health(monkeypatch, {"webhook_hmac_configured": True}, local_secret=None)

    assert result.verdict == "signature_mismatch"
    assert result.status_code == 200
    assert "401" in result.detail
    assert "REGENGINE_WEBHOOK_HMAC_SECRET" in result.detail
    assert "unset here" in result.detail


def test_a_local_secret_the_remote_never_verifies_is_a_signature_mismatch(monkeypatch: Any) -> None:
    """The opposite direction is quieter and just as wrong: this side
    signs, RegEngine's verifier no-ops, and bodies go unverified."""
    result = _probe_with_health(monkeypatch, {"webhook_hmac_configured": False}, local_secret="shared-secret")

    assert result.verdict == "signature_mismatch"
    assert "unverified" in result.detail
    assert "REGENGINE_WEBHOOK_HMAC_SECRET" in result.detail


@pytest.mark.parametrize(
    "remote_flag,local_secret",
    [(True, "shared-secret"), (False, None), ("true", "shared-secret"), ("false", None)],
)
def test_agreeing_signing_postures_stay_connected(monkeypatch: Any, remote_flag: Any, local_secret: str | None) -> None:
    """No false positive when the two sides agree, whether RegEngine spells
    the flag as a boolean or as its string form."""
    result = _probe_with_health(monkeypatch, {"webhook_hmac_configured": remote_flag}, local_secret=local_secret)

    assert result.verdict == "connected"


@pytest.mark.parametrize(
    "health_payload",
    [
        {"inflow_contract_version": "1"},  # an older RegEngine: no posture field at all
        {"webhook_hmac_configured": "sometimes"},  # unrecognised spelling
        {"webhook_hmac_configured": None},
        None,  # /health unreachable or not JSON
        "not a document",
    ],
)
def test_an_unadvertised_posture_never_produces_a_verdict(monkeypatch: Any, health_payload: Any) -> None:
    """The probe must not invent a failure it cannot see: with no readable
    posture flag the verdict is exactly what it was before #217, whatever
    the local secret says."""
    from app.contract import INFLOW_CONTRACT_VERSION

    if isinstance(health_payload, dict) and "inflow_contract_version" in health_payload:
        health_payload = {"inflow_contract_version": INFLOW_CONTRACT_VERSION}
    result = _probe_with_health(monkeypatch, health_payload, local_secret="shared-secret")

    assert result.verdict == "connected"
    assert "does not confirm" in result.detail.lower()


def test_contract_mismatch_outranks_signature_mismatch(monkeypatch: Any) -> None:
    """A contract skew makes every payload wrong regardless of how it is
    signed, so it keeps precedence when both disagree at once -- which also
    keeps the existing contract_mismatch pin above intact."""
    from app.contract import INFLOW_CONTRACT_VERSION

    result = _probe_with_health(
        monkeypatch,
        {"inflow_contract_version": f"{INFLOW_CONTRACT_VERSION}-stale-test", "webhook_hmac_configured": True},
        local_secret=None,
    )

    assert result.verdict == "contract_mismatch"


def test_a_non_200_probe_is_never_upgraded_to_signature_mismatch(monkeypatch: Any) -> None:
    """The posture comparison only refines a green read. A 401 stays a 401
    even when the postures also disagree: the operator's first problem is
    the credential, and saying "signature" would send them the wrong way."""
    from app.regengine_client import WEBHOOK_HMAC_SECRET_ENV

    monkeypatch.delenv(WEBHOOK_HMAC_SECRET_ENV, raising=False)
    fake = _fake_probe_client(status_code=401, health_payload={"webhook_hmac_configured": True})
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)

    result = asyncio.run(LiveRegEngineClient().check_connection(_live_config()))

    assert result.verdict == "unauthorized"


# ---------------------------------------------------------------------------
# Constraints this fix must not weaken: the egress guard (#87) and the
# error-body masking (#138)
# ---------------------------------------------------------------------------


def test_check_connection_still_refuses_a_private_endpoint_before_dialing(monkeypatch: Any) -> None:
    """#87's guard must still run first: check_connection must raise
    EgressBlockedError, and httpx.AsyncClient must never even be
    constructed, for a delivery endpoint that resolves to a blocked
    address -- unchanged by this file's edits to the same function."""
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", _UnreachableAsyncClient)
    config = _live_config(endpoint="http://169.254.169.254/latest/meta-data/")

    with pytest.raises(EgressBlockedError):
        asyncio.run(LiveRegEngineClient().check_connection(config))


def test_extract_error_body_still_masks_the_api_key_and_truncates_long_bodies() -> None:
    """#138's guarantees on the shared error-body helper: an api_key
    echoed back inside RegEngine's free-text error must never survive
    into the returned string, and an oversized body must be bounded --
    both unrelated to, and unweakened by, this file's check_connection
    changes."""
    api_key = "rge_live_super_secret_probe_key"

    class _EchoResponse:
        status_code = 401

        def json(self) -> Any:
            raise ValueError("not json")

        @property
        def text(self) -> str:
            return f"invalid key {api_key} for this tenant"

    masked = _extract_error_body(_EchoResponse(), api_key)
    assert api_key not in masked
    assert MASKED_SECRET in masked

    class _HugeResponse:
        status_code = 500

        def json(self) -> Any:
            raise ValueError("not json")

        @property
        def text(self) -> str:
            return "x" * (_MAX_ERROR_BODY_CHARS * 2)

    bounded = _extract_error_body(_HugeResponse(), None)
    assert len(bounded) <= _MAX_ERROR_BODY_CHARS + 100
    assert "truncated" in bounded


# ---------------------------------------------------------------------------
# End to end through POST /api/integration/test -- proves
# app/routers/integration.py (not owned by this change, not modified by
# it) forwards the reworded detail text through to the console unchanged
# ---------------------------------------------------------------------------


def test_integration_test_endpoint_forwards_the_reworded_connected_detail(monkeypatch: Any) -> None:
    fake = _fake_probe_client(status_code=200)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", fake)

    response = client.post(
        "/api/integration/test",
        json={"api_key": "rge_live_probe_key", "tenant_id": "33333333-3333-3333-3333-333333333333"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "connected"
    detail = body["detail"].lower()
    assert "does not confirm" in detail
    assert "subscription" in detail
    assert "webhooks.ingest" in detail
    assert "rge_live_probe_key" not in body["detail"]

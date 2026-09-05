from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import getproxies

import httpx

from .contract import INFLOW_CONTRACT_VERSION
from .schemas.ingestion import IngestPayload
from .schemas.simulation import SimulationConfig, async_resolve_egress_endpoint
# Same masking convention store.py persists with and controller.py logs
# with -- see _extract_error_body below (#138).
from .store import mask_secret_in_payload, mask_secret_in_string


DEFAULT_LIVE_INGEST_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"
DEFAULT_LIVE_TIMEOUT_SECONDS = 30.0

# Cap on how much of a failed live ingest's response body (#138) is kept
# in the raised error / delivery_metadata. RegEngine's real validation
# errors are compact per-event JSON, comfortably under this; the cap is
# for the pathological case -- an HTML error page from a misconfigured
# proxy, or an oversized body some other layer streams back -- so one bad
# response can't bloat the stored event record or the console log without
# limit.
_MAX_ERROR_BODY_CHARS = 4000

# Env var used to share an HMAC secret with the RegEngine ingest service.
# When set, every live ingest request is signed with HMAC-SHA256 over the
# exact request body bytes and sent as `X-Webhook-Signature: sha256=<hex>`.
# When unset, signing is skipped — RegEngine's _verify_webhook_signature
# also no-ops when its WEBHOOK_HMAC_SECRET is unset, so this preserves the
# pre-signing migration ramp on both sides.
# Bandit B105 false positive: this is an env var key, not a secret literal.
WEBHOOK_HMAC_SECRET_ENV = "REGENGINE_WEBHOOK_HMAC_SECRET"  # nosec B105


@dataclass(frozen=True, slots=True)
class LiveIngestResult:
    response: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConnectionCheckResult:
    verdict: str
    detail: str
    status_code: int | None = None
    endpoint_host: str = ""


# Interpretation of the authenticated read probe, keyed by HTTP status.
# These mirror the failure modes RegEngine's webhook surface actually
# returns, so the settings page can give recovery guidance instead of a
# bare status code.
#
# #100 -- this maps GET /api/v1/webhooks/recent's status code, NOT
# POST /api/v1/webhooks/ingest's. The two routes' dependency chains
# differ: /recent authenticates the caller and checks a read-scope
# permission, but never touches the tenant's subscription status, the
# separate webhooks.ingest permission scope, or (when configured) the
# HMAC request signature -- all three are gates on /ingest alone. A
# credential that gets HTTP 200 here can still fail an actual ingest
# with 401 (signature), 402 (subscription) or 403 (wrong scope).
#
# The verdict string for HTTP 200 deliberately stays "connected":
# tests/test_integration_settings.py, tests/test_egress_guard.py and
# tests/test_client_diagnostics.py all pin it there (as does
# scripts/customer_journey.py's live smoke check), and none of those
# files are this change's to edit. So the fix for the false-assurance
# report below is in the DETAIL text, not the verdict: say plainly what
# a green read proves and what it does not, instead of implying ingest
# is safe. A verdict that actually named the gap (e.g. "read_only") was
# considered and rejected for exactly that reason -- see
# tests/test_connection_probe.py for the full reasoning, including what
# a RegEngine-side change would need to add for a verdict-level fix.
#
# #217 -- one of those three gates has since become observable without
# writing: RegEngine's /health advertises `webhook_hmac_configured`, in
# the same document _fetch_remote_health already reads for the contract
# version. When RegEngine advertises it and it disagrees with whether
# REGENGINE_WEBHOOK_HMAC_SECRET is set here, a 200 probe is reported as
# SIGNATURE_MISMATCH_VERDICT instead of "connected" (see
# _signature_posture_detail). When the field is absent -- an older
# RegEngine, or a /health that is not JSON -- the verdict is left alone
# rather than guessed: the probe never invents a failure it cannot see.
_CONNECTION_VERDICTS: dict[int, tuple[str, str]] = {
    200: (
        "connected",
        "Authenticated read succeeded: this API key and tenant can read "
        "RegEngine's webhook surface. This does not confirm that POST "
        "/api/v1/webhooks/ingest will accept events -- that route gates on "
        "three things a read-only probe cannot reach: the tenant's "
        "subscription status, the separate webhooks.ingest permission "
        "scope (a read-only key passes this probe and then gets HTTP 403 "
        "on ingest), and -- if RegEngine requires it -- the HMAC request "
        "signature (a signing-posture disagreement is reported separately "
        "as signature_mismatch when RegEngine advertises its posture). If "
        "live ingests fail after this reports connected, check those three "
        "first.",
    ),
    401: ("unauthorized", "RegEngine rejected the API key (HTTP 401). Check the key value and that it has not expired or been revoked."),
    # #100: per the issue's route audit, RegEngine's subscription gate
    # (require_active_subscription) sits on /ingest only, so this probe
    # returning 402 is not currently expected. Kept rather than deleted:
    # it is the correct verdict if that ever changes, an unreachable
    # entry costs nothing, and it is already pinned by
    # tests/test_client_diagnostics.py and tests/test_integration_settings.py.
    402: ("subscription_inactive", "The tenant's subscription is not active (HTTP 402). Billing status must be active or trialing before ingestion resumes."),
    403: ("forbidden", "RegEngine refused the request (HTTP 403). The key may be missing the webhooks.read scope, or the tenant does not match the key."),
    404: ("tenant_mismatch", "RegEngine returned HTTP 404. Webhook reads return 404 when the tenant does not match the API key's tenant."),
    429: ("rate_limited", "RegEngine is rate limiting this tenant (HTTP 429). Wait for the window shown in Retry-After and try again."),
    503: ("service_unavailable", "RegEngine is up but a required backing service is unavailable (HTTP 503). Try again shortly."),
}

# Verdict for a 200 probe whose signing posture disagrees with RegEngine's
# advertised one (#217). Not keyed by status code because it is not one:
# it is an upgrade applied on top of "connected", the way
# "contract_mismatch" is, and it ranks below that one because a contract
# skew makes every payload wrong regardless of how it is signed.
SIGNATURE_MISMATCH_VERDICT = "signature_mismatch"


class LiveRegEngineDeliveryError(RuntimeError):
    def __init__(self, message: str, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.metadata = metadata


class LiveRegEngineClient:
    async def ingest(
        self,
        payload: IngestPayload,
        config: SimulationConfig,
        idempotency_key: str | None = None,
    ) -> LiveIngestResult:
        endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
        api_key = config.delivery.api_key
        tenant_id = config.delivery.tenant_id
        if not api_key or not tenant_id:
            raise ValueError("Live delivery requires both api_key and tenant_id")
        # SSRF/credential-exfiltration guard — see resolve_egress_endpoint's
        # docstring. This is the enforcement point: the check sits immediately
        # before the request, and the address it validated comes back so
        # _pinned_dial below can aim the POST at THAT address instead of
        # letting httpx resolve the hostname a second time on its own -- the
        # second lookup a hostile zone answered with 127.0.0.1 to walk through
        # this guard entirely (#207). None means there is nothing to pin, and
        # the request is dialed by hostname exactly as before.
        pinned_address = await async_resolve_egress_endpoint(config.delivery.endpoint)

        idempotency_key = idempotency_key or uuid.uuid4().hex
        # Serialize the body exactly once so the bytes we sign are the same
        # bytes httpx puts on the wire. If we passed json=payload.model_dump()
        # to httpx, it would re-serialize and any whitespace/key-order drift
        # between our HMAC input and the wire body would cause RegEngine's
        # signature check to 401 on every request.
        body_bytes = json.dumps(
            payload.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        signature_header = _build_signature_header(body_bytes)

        metadata = _delivery_metadata(
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            signed=signature_header is not None,
        )
        headers = {
            "Content-Type": "application/json",
            "X-RegEngine-API-Key": api_key,
            "X-Tenant-ID": tenant_id,
            "Idempotency-Key": idempotency_key,
        }
        if signature_header is not None:
            headers["X-Webhook-Signature"] = signature_header
        dial_url, dial_headers, dial_kwargs = _pinned_dial(endpoint, headers, pinned_address)
        client_kwargs = _pinned_client_kwargs(endpoint, pinned_address)
        try:
            async with httpx.AsyncClient(timeout=_live_timeout_seconds(), **client_kwargs) as client:
                response = await client.post(
                    dial_url, headers=dial_headers, content=body_bytes, **dial_kwargs
                )
        except httpx.HTTPError as exc:
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc

        metadata = metadata | {"status_code": response.status_code}
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # #138: str(exc) alone is only httpx's generic status-line text
            # (e.g. "Client error '402 Payment Required' for url ...") --
            # RegEngine's actual per-event rejection reason lives in the
            # response body, which used to be discarded here. Attach it
            # (masked and bounded -- see _extract_error_body) to both the
            # raised message and the metadata so the console and the
            # stored delivery record show *why*, not just the status code.
            error_body = _extract_error_body(exc.response, api_key)
            message = f"{exc} | RegEngine response: {error_body}" if error_body else str(exc)
            raise LiveRegEngineDeliveryError(message, metadata | {"error_body": error_body}) from exc
        return LiveIngestResult(response=response.json(), metadata=metadata)

    async def check_connection(self, config: SimulationConfig) -> ConnectionCheckResult:
        """Probe RegEngine with the configured credentials without ingesting.

        Uses GET /api/v1/webhooks/recent?limit=1 — the cheapest authenticated
        read on the webhook surface — and maps the response to a failure
        vocabulary keyed by HTTP status (bad key, wrong tenant, rate limit).

        #100: this is deliberately a read, never a write -- posting a
        synthetic event into a customer's production RegEngine to "test"
        the connection is not acceptable for a button labelled Test
        connection. The tradeoff that buys is real: /recent's dependency
        chain is weaker than /ingest's, so this cannot observe the
        tenant's subscription status, the webhooks.ingest permission scope,
        or (when configured) the HMAC signature requirement -- three gates
        that live only on the write path and can each independently fail
        an ingest a passing probe here said nothing about. See
        _CONNECTION_VERDICTS' comment and tests/test_connection_probe.py
        for how the HTTP-200 case is worded to reflect that gap instead of
        implying ingest is safe.
        """
        endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
        api_key = config.delivery.api_key
        tenant_id = config.delivery.tenant_id
        parsed = urlparse(endpoint)
        host = parsed.netloc
        if not api_key or not tenant_id:
            return ConnectionCheckResult(
                verdict="not_configured",
                detail="Both an API key and a tenant id are required before testing the connection.",
                endpoint_host=host,
            )
        # SSRF/credential-exfiltration guard — see resolve_egress_endpoint's
        # docstring. Enforced here, immediately before the probe goes out, and
        # as in ingest() above the validated address is pinned for the dial,
        # so the address checked IS the address connected to and the hostname
        # is resolved exactly once (#207). Both requests this method makes --
        # the /recent probe and the /health contract read -- go through the
        # same pin, so neither of them re-resolves. The caller (the /test
        # route) turns a raised EgressBlockedError into a clean 4xx rather
        # than letting it become an unhandled 500.
        pinned_address = await async_resolve_egress_endpoint(config.delivery.endpoint)

        probe_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/webhooks/recent"
        headers = {
            "X-RegEngine-API-Key": api_key,
            "X-Tenant-ID": tenant_id,
        }
        dial_url, dial_headers, dial_kwargs = _pinned_dial(probe_url, headers, pinned_address)
        client_kwargs = _pinned_client_kwargs(probe_url, pinned_address)
        try:
            async with httpx.AsyncClient(timeout=_live_timeout_seconds(), **client_kwargs) as client:
                response = await client.get(
                    dial_url,
                    headers=dial_headers,
                    params={"tenant_id": tenant_id, "limit": 1},
                    **dial_kwargs,
                )
                remote_health = await _fetch_remote_health(client, parsed, pinned_address)
        except httpx.HTTPError as exc:
            return ConnectionCheckResult(
                verdict="unreachable",
                detail=f"Could not reach RegEngine at {host}: {exc.__class__.__name__}",
                endpoint_host=host,
            )

        verdict, detail = _CONNECTION_VERDICTS.get(
            response.status_code,
            ("error", f"Unexpected HTTP {response.status_code} from RegEngine."),
        )
        remote_contract = _remote_contract_version(remote_health)
        if verdict == "connected" and remote_contract is not None and remote_contract != INFLOW_CONTRACT_VERSION:
            verdict = "contract_mismatch"
            detail = (
                f"Credentials are valid, but this simulator implements ingest contract "
                f"v{INFLOW_CONTRACT_VERSION} while RegEngine advertises v{remote_contract}. "
                "One side is running an older deploy — update it before ingesting, or "
                "payloads may validate here and be rejected there (or vice versa)."
            )
        if verdict == "connected":
            # The read probe cannot see the ingest route's signature check,
            # so compare signing posture across the two sides instead: the
            # one ingest-only gate observable without writing, and the one
            # most likely to be half-configured in a shared demo (#217).
            # Ranked after the contract check on purpose -- see
            # SIGNATURE_MISMATCH_VERDICT.
            signature_detail = _signature_posture_detail(_remote_hmac_configured(remote_health))
            if signature_detail is not None:
                verdict = SIGNATURE_MISMATCH_VERDICT
                detail = signature_detail
        return ConnectionCheckResult(
            verdict=verdict,
            detail=detail,
            status_code=response.status_code,
            endpoint_host=host,
        )


async def _fetch_remote_health(
    client: httpx.AsyncClient, parsed, pinned_address: str | None = None
) -> dict[str, Any] | None:
    """Best-effort read of RegEngine's /health document.

    Returns None when the health endpoint is unreachable or non-JSON (some
    deployments serve the frontend at /health). Every skew check built on
    it -- the contract version, the signing posture -- engages only when
    the remote actually advertises the field it needs, so an older
    RegEngine simply yields no extra verdict.

    Takes the caller's already-validated *pinned_address* so this second
    request reuses the one lookup check_connection made rather than
    resolving the host again on its own (#207).
    """
    health_url, health_headers, health_kwargs = _pinned_dial(
        f"{parsed.scheme}://{parsed.netloc}/health", {}, pinned_address
    )
    try:
        health = await client.get(health_url, headers=health_headers, **health_kwargs)
        payload = health.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _remote_contract_version(health: dict[str, Any] | None) -> str | None:
    """RegEngine's advertised inflow contract version, when it advertises one."""
    if health is None:
        return None
    value = health.get("inflow_contract_version")
    return str(value) if value is not None else None


# Key names RegEngine's /health may use for its webhook-signing posture
# flag (`webhook_hmac_configured` today, per its routes_health_metrics).
# Only a real boolean, or its string spelling, counts: a missing or
# unrecognised value leaves the posture unknown, and unknown never
# produces a verdict (#217).
_REMOTE_HMAC_KEYS = ("webhook_hmac_configured", "hmac_configured")
_TRUTHY_POSTURE = {"1", "true", "yes", "on"}
_FALSY_POSTURE = {"0", "false", "no", "off"}


def _remote_hmac_configured(health: dict[str, Any] | None) -> bool | None:
    """Whether RegEngine says it verifies webhook signatures; None if unknown."""
    if health is None:
        return None
    for key in _REMOTE_HMAC_KEYS:
        value = health.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _TRUTHY_POSTURE:
                return True
            if normalized in _FALSY_POSTURE:
                return False
    return None


def _signature_posture_detail(remote_hmac_configured: bool | None) -> str | None:
    """Explain a webhook-signing mismatch between the two sides, if any.

    Returns None when the posture matches or when RegEngine does not
    advertise its posture at all -- the probe never invents a failure it
    cannot see. Both directions are named because both bite: a remote
    that verifies while this side does not sign rejects every ingest with
    HTTP 401, and a remote that does not verify while this side signs
    accepts bodies nobody checked.
    """
    if remote_hmac_configured is None:
        return None
    local_configured = bool(os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip())
    if local_configured == remote_hmac_configured:
        return None
    if remote_hmac_configured:
        return (
            "Credentials are valid for reads, but RegEngine has webhook HMAC signing "
            f"enabled while {WEBHOOK_HMAC_SECRET_ENV} is unset here. Every live ingest "
            "would be sent unsigned and rejected with HTTP 401 -- the read probe cannot "
            "see this because only the ingest route verifies signatures. Set the same "
            "secret on both sides."
        )
    return (
        f"Credentials are valid for reads, but {WEBHOOK_HMAC_SECRET_ENV} is set here while "
        "RegEngine reports webhook HMAC signing disabled. Ingest still succeeds and "
        "RegEngine's verifier no-ops, so bodies go unverified -- set WEBHOOK_HMAC_SECRET on "
        "RegEngine, or clear the local secret, so both sides agree."
    )


def _extract_error_body(response: httpx.Response, api_key: str | None) -> str:
    """Best-effort, masked, bounded rendering of RegEngine's error body.

    RegEngine's real validation failures come back as JSON carrying
    per-event rejection reasons (see the webhook contract this pins
    against) -- that detail is what an operator actually needs to fix a
    failed live delivery, and #138 is exactly that it used to be thrown
    away in favor of httpx's generic status-line text.

    Two hard constraints this enforces before the text goes anywhere:
    - Masked. RegEngine's body can echo request details back inside free
      text (e.g. an "invalid key <value>" detail message), so this cannot
      rely on store.py's _scrub_secrets alone -- that only redacts a
      secret sitting in a field literally named api_key/authorization/etc
      at persistence time, not one embedded in prose anywhere else. This
      uses the same mask_secret_in_payload/mask_secret_in_string store.py
      and controller.py already mask with, which strip the configured key
      by value, wherever it appears, before this ever leaves the client.
    - Bounded. A non-JSON error body (an HTML error page from a
      misconfigured proxy, for instance) could be arbitrarily large;
      _MAX_ERROR_BODY_CHARS caps what is kept so a pathological response
      can't bloat the raised message or the stored delivery record.

    Never raises: a body that is not JSON and cannot be decoded as text
    degrades to a placeholder rather than masking the original
    HTTPStatusError behind a secondary exception.
    """
    try:
        parsed: Any = response.json()
    except ValueError:
        parsed = None

    if parsed is not None:
        text = json.dumps(mask_secret_in_payload(parsed, api_key), sort_keys=True)
    else:
        try:
            text = mask_secret_in_string(response.text, api_key) or ""
        except Exception:
            return "<error body unavailable>"

    if len(text) > _MAX_ERROR_BODY_CHARS:
        omitted = len(text) - _MAX_ERROR_BODY_CHARS
        text = f"{text[:_MAX_ERROR_BODY_CHARS]}... [truncated, {omitted} more chars]"
    return text


def _proxy_is_configured(url: httpx.URL) -> bool:
    """Would httpx send *url* through an HTTP proxy?

    This decides whether the connect-time pin (#207) applies, and it must
    never answer False when httpx would in fact proxy. With a proxy the
    socket is opened by the proxy, so a pinned IP becomes the CONNECT
    target -- and httpcore's tunnel connection ignores the sni_hostname
    extension entirely, verifying the certificate against that tunnel
    target, i.e. the IP. No ordinary certificate satisfies that, and every
    obvious "fix" for it ends in weakened verification, which is a worse
    security outcome than the rebinding gap #207 closes. So: proxied means
    dial by hostname, exactly as before.

    Answered with httpx's own machinery rather than a hand-rolled NO_PROXY
    matcher, because it has to agree with httpx exactly. These are the same
    two pieces ``httpx.AsyncClient(trust_env=True)`` -- the default this
    module uses -- builds its proxy mounts from, applied in httpx's own
    order: mounts sorted most-specific-first, first match wins
    (``Client._init_proxy_map`` and ``Client._transport_for_url``). A test
    in tests/test_egress_guard.py cross-checks this function against a real
    client's transport choice, so an httpx release that moves or changes
    them fails CI loudly instead of silently mis-deciding.

    The pin is decided by asking this about the ORIGINAL URL -- the one the
    operator configured -- because that is the URL whose proxy routing
    expresses their policy. httpx, though, chooses a transport from the
    URL it is actually handed, and pinning rewrites that URL's host to an
    IP: a NO_PROXY entry naming a hostname does not cover the address that
    hostname resolves to, so an environment that exempts ``pypi.org`` from
    its proxy still sends ``https://151.101.192.223/...`` through it. That
    is precisely the tunneled-pin case that must never happen, and it is
    why _pinned_client_kwargs asks this a second time about the pinned URL:
    when the name goes direct but its address would be tunneled, the
    client gets an explicit direct mount for that address, so the request
    reaches the wire exactly as the operator's NO_PROXY said the host
    should (#217; measured against a live proxy, see
    tests/test_egress_guard.py).

    If that import ever disappears the fallback is the coarsest safe
    answer: any proxy configured for the scheme counts as proxied, NO_PROXY
    unread. That errs only in the safe direction -- it can skip a pin that
    would have been fine, never pin a request that then gets tunneled.

    What skipping costs, for the one shape that still skips, is honest and
    bounded: fully proxied, this process performs no lookup for the
    connection at all (the proxy resolves), so the two disagreeing lookups
    a rebinding attack needs do not exist on this side either. The guard's
    own lookup becomes advisory and what the proxy connects to is the
    proxy's egress policy to enforce.
    """
    try:
        from httpx._utils import URLPattern, get_environment_proxies
    except ImportError:  # pragma: no cover - only on an httpx that moved these
        proxies = getproxies()
        return bool(proxies.get(url.scheme.lower()) or proxies.get("all"))
    mounts = get_environment_proxies()
    for pattern in sorted(URLPattern(key) for key in mounts):
        if pattern.matches(url):
            # A None mount is httpx's spelling of "NO_PROXY covers this".
            return mounts[pattern.pattern] is not None
    return False


def _pinned_dial(
    endpoint: str,
    headers: dict[str, str],
    pinned_address: str | None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Aim one request at *pinned_address* while it still speaks to *endpoint*'s host.

    Returns the ``(url, headers, extra kwargs)`` to hand httpx. With no
    address to pin -- or with a proxy in the way, see _proxy_is_configured --
    it returns the caller's own arguments untouched, so the un-pinned path is
    byte-for-byte the request this client has always sent.

    When it does pin, three things move together and all three matter:

      * The URL's host becomes the validated IP, so httpx opens the socket to
        that address and never performs a lookup of its own. This is the
        actual fix for #207: the address the guard checked is the address
        connected to. Rewriting the host also re-decides httpx's proxy
        routing, which _pinned_client_kwargs corrects with an explicit
        direct mount when the operator's NO_PROXY exempts the name but not
        its address -- see _proxy_is_configured.
      * ``Host`` is set explicitly to the ORIGINAL netloc -- host plus port
        when the port is not the scheme default -- which is exactly what
        httpx auto-populates from an un-pinned URL (``Request._prepare`` uses
        ``url.netloc``). Sending the bare hostname instead would break any
        endpoint on a non-default port, and sending the IP would break
        name-based virtual hosting.
      * ``sni_hostname`` is the original bare hostname. httpcore passes it to
        ``start_tls`` as ``server_hostname``, and httpx's default SSL context
        is ``ssl.create_default_context()`` with ``check_hostname`` on, so
        that one value drives BOTH the SNI extension and the certificate
        hostname check. The certificate is therefore verified against the
        name the operator configured, never against the pinned IP -- nothing
        here relaxes verification, and there is no code path in this module
        that sets ``verify=False`` or hands httpx a permissive SSL context.
    """
    if not pinned_address:
        return endpoint, headers, {}
    url = httpx.URL(endpoint)
    if _proxy_is_configured(url):
        return endpoint, headers, {}
    pinned_url = url.copy_with(host=pinned_address)
    return (
        str(pinned_url),
        {**headers, "Host": url.netloc.decode("ascii")},
        {"extensions": {"sni_hostname": url.host}},
    )


def _pinned_client_kwargs(endpoint: str, pinned_address: str | None) -> dict[str, Any]:
    """Extra ``httpx.AsyncClient`` arguments a pinned dial needs, if any.

    Empty in every shape but one. When the operator's environment proxies
    traffic in general yet exempts this endpoint's HOSTNAME through
    NO_PROXY, the request is meant to go direct -- and it would, dialled by
    name. Dialled by the pinned address it would not: httpx picks a
    transport from the URL it is handed, the address is not in NO_PROXY,
    and the pinned request would be tunneled through the proxy, which
    verifies the certificate against the tunnel target (the IP) and fails.
    Skipping the pin there left #207's gap open for exactly the endpoints
    an operator had singled out as direct (#217).

    So this mounts an explicit direct transport for the pinned address on
    the client. httpx keeps building its environment proxy mounts alongside
    (``trust_env`` is untouched -- only passing ``transport=`` would disable
    them), and a host-specific mount outranks the scheme-wide proxy mounts
    in its own precedence order, so: this one address goes direct, the
    hostname's NO_PROXY exemption is honoured for the connection actually
    made, and every other host still follows the proxy. The direct
    transport is httpx's own default -- default SSL context, hostname
    verification on -- so pinning changes where the socket goes and nothing
    about how it is verified.
    """
    if not pinned_address:
        return {}
    url = httpx.URL(endpoint)
    if _proxy_is_configured(url):
        return {}
    pinned_url = url.copy_with(host=pinned_address)
    if not _proxy_is_configured(pinned_url):
        return {}
    # netloc keeps the brackets an IPv6 literal needs and the explicit port
    # when there is one, which is also what the pinned request URL carries.
    return {"mounts": {f"all://{pinned_url.netloc.decode('ascii')}": httpx.AsyncHTTPTransport()}}


def _build_signature_header(body_bytes: bytes) -> str | None:
    """Build the X-Webhook-Signature header value, or None when unsigned.

    Returns None when REGENGINE_WEBHOOK_HMAC_SECRET is unset or empty so
    deployments still mid-migration to signed webhooks continue to work.
    Production deployments MUST set the secret on both sides; RegEngine's
    webhook router will reject signed-required requests without a matching
    signature.
    """
    secret = os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip()
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _live_timeout_seconds() -> float:
    raw = os.getenv("REGENGINE_LIVE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_LIVE_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        return DEFAULT_LIVE_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_LIVE_TIMEOUT_SECONDS


def _delivery_metadata(
    endpoint: str,
    idempotency_key: str,
    signed: bool = False,
) -> dict[str, Any]:
    parsed = urlparse(endpoint)
    return {
        "delivery_mode": "live",
        "endpoint_host": parsed.netloc,
        "endpoint_path": parsed.path or "/",
        "idempotency_key": idempotency_key,
        "signed": signed,
        "status_code": None,
    }

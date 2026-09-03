from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .contract import INFLOW_CONTRACT_VERSION
from .schemas.ingestion import IngestPayload
from .schemas.simulation import SimulationConfig

logger = logging.getLogger(__name__)


DEFAULT_LIVE_INGEST_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"
DEFAULT_LIVE_TIMEOUT_SECONDS = 30.0

# Env var used to share an HMAC secret with the RegEngine ingest service.
# When set, every live ingest request is signed with HMAC-SHA256 over the
# exact request body bytes and sent as `X-Webhook-Signature: sha256=<hex>`.
# When unset, signing is skipped — RegEngine's _verify_webhook_signature
# also no-ops when its WEBHOOK_HMAC_SECRET is unset, so this preserves the
# pre-signing migration ramp on both sides.
# Bandit B105 false positive: this is an env var key, not a secret literal.
WEBHOOK_HMAC_SECRET_ENV = "REGENGINE_WEBHOOK_HMAC_SECRET"  # nosec B105

# --- Egress restriction (SSRF / credential-exfiltration guard) -------------
#
# Delivery endpoints are caller-supplied (POST /api/integration/test, and the
# inline `delivery` block accepted by the CSV-import, replay and demo-fixture
# routes). Every outbound call carries the RegEngine API key and tenant id in
# request headers, so an unrestricted endpoint is both an SSRF pivot and a
# credential-exfiltration channel. Both outbound paths -- ingest() and
# check_connection() -- validate the endpoint before sending anything.
#
# Validation alone is not enough, because validating a *name* and then handing
# that same name to httpx means the name is resolved twice, independently. A
# hostile zone with a short TTL answers public for the guard's lookup and
# loopback for httpx's, and the credentials follow the second answer (#207).
# So the guard resolves once and returns the address it approved, and the
# client dials *that address* -- see PinnedEndpoint and _PinnedAddressTransport
# below. There is no second resolution to poison.
#
# Optional strict allowlist: comma-separated hosts. A leading dot matches
# subdomains (".regengine.co" allows "www.regengine.co"). When unset, any
# public host is allowed but private/loopback/link-local destinations are not.
ALLOWED_DELIVERY_HOSTS_ENV = "REGENGINE_ALLOWED_DELIVERY_HOSTS"
# Escape hatch for developers pointing the simulator at a RegEngine stack
# running on localhost (see scripts/customer_journey.py). Off by default.
ALLOW_PRIVATE_DELIVERY_ENV = "REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS"
# DNS resolution of non-literal hosts can be disabled in sealed environments.
DELIVERY_DNS_GUARD_ENV = "REGENGINE_DELIVERY_DNS_GUARD"
# Opt back in to cleartext (`http://`) delivery to a public host. Off by
# default: every live delivery and probe carries the API key in an
# `Authorization` header, and over `http` that header crosses the network in
# the clear. `REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS` already implies it, so a
# local stack on `http://localhost:8000` needs nothing new.
ALLOW_CLEARTEXT_DELIVERY_ENV = "REGENGINE_ALLOW_CLEARTEXT_DELIVERY"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
_BLOCKED_HOST_SUFFIXES = (".localhost", ".internal", ".local")
BLOCKED_ENDPOINT_VERDICT = "blocked_endpoint"


class BlockedDeliveryEndpointError(ValueError):
    """Raised when a delivery endpoint is not an allowed egress destination."""


@dataclass(frozen=True, slots=True)
class PinnedEndpoint:
    """The one address the guard approved, plus the name it approved it for.

    `hostname` is the endpoint's original hostname and stays authoritative for
    everything name-shaped: the `Host` header, TLS SNI and therefore
    certificate verification. `address` is the literal the socket is opened to.
    """

    hostname: str
    address: str


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _allowed_delivery_hosts() -> list[str]:
    raw = os.getenv(ALLOWED_DELIVERY_HOSTS_ENV, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    for entry in allowlist:
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def _is_blocked_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
        return True
    return ip.is_multicast or ip.is_unspecified


def _resolved_addresses(host: str) -> list[str]:
    """Best-effort blocking DNS resolution, for synchronous callers.

    A host that does not resolve cannot be reached, so an unresolvable name is
    not a bypass -- it is left to fail as an ordinary connection error rather
    than being reported as a blocked endpoint.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return [info[4][0] for info in infos]


async def _aresolved_addresses(host: str) -> list[str]:
    """`_resolved_addresses` without blocking the event loop.

    `socket.getaddrinfo` blocks for the full resolver timeout, and the guard
    runs on every live delivery and every connection test, so doing it inline
    in an `async def` stalls every other request in the process behind a slow
    or unresponsive resolver. The loop's own `getaddrinfo` runs the same call
    in its executor and yields meanwhile; failures degrade exactly as above.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _static_endpoint_checks(endpoint: str) -> str | None:
    """Every check that needs no resolver, cheapest first.

    Returns the hostname still awaiting DNS validation, or None when the
    endpoint is already fully checked (IP literal, opt-out in force, DNS guard
    disabled). Raises BlockedDeliveryEndpointError for a refused endpoint.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise BlockedDeliveryEndpointError(
            "Delivery endpoint must be an http(s) URL."
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BlockedDeliveryEndpointError("Delivery endpoint must include a host.")

    allowlist = _allowed_delivery_hosts()
    if allowlist and not _host_matches_allowlist(host, allowlist):
        raise BlockedDeliveryEndpointError(
            f"Delivery endpoint host {host!r} is not in {ALLOWED_DELIVERY_HOSTS_ENV}."
        )

    if _env_flag(ALLOW_PRIVATE_DELIVERY_ENV):
        # Local-development opt-out: the operator has deliberately pointed the
        # simulator at a private stack (localhost, the built-in mock, a LAN
        # host). Nothing is resolved and nothing is pinned -- there is no
        # address the guard would refuse, so pinning would only make this path
        # behave differently from a plain httpx call.
        return None

    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise BlockedDeliveryEndpointError(
            f"Delivery endpoint host {host!r} resolves to a local/internal "
            "destination. Set "
            f"{ALLOW_PRIVATE_DELIVERY_ENV}=1 only for a trusted local stack."
        )

    literal = host.strip("[]")
    if _is_blocked_address(literal):
        raise BlockedDeliveryEndpointError(
            f"Delivery endpoint address {literal} is loopback/private/link-local "
            "and is not an allowed destination. Set "
            f"{ALLOW_PRIVATE_DELIVERY_ENV}=1 only for a trusted local stack."
        )
    if parsed.scheme == "http" and not _env_flag(ALLOW_CLEARTEXT_DELIVERY_ENV):
        # Every check that names a *specific* unsafe destination has run and
        # passed, so this is an ordinary public host and the only thing wrong
        # with it is the scheme. Ordered last of the static checks deliberately:
        # a metadata or loopback endpoint that also happens to be cleartext
        # should be reported as the local/internal destination it is, because
        # that is the finding the operator has to act on.
        #
        # Refused before any credential header is built and before the name is
        # resolved -- over `http` the API key crosses the network in the clear.
        raise BlockedDeliveryEndpointError(
            f"Delivery endpoint {endpoint!r} is cleartext http. The API key "
            "would be sent unencrypted. Use https, or set "
            f"{ALLOW_CLEARTEXT_DELIVERY_ENV}=1 for a trusted network."
        )

    try:
        ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        # Literal address already checked, and the URL already carries it:
        # there is no name for a resolver to answer differently later.
        return None

    if os.getenv(DELIVERY_DNS_GUARD_ENV, "1").strip().lower() not in _TRUTHY:
        return None
    return host


def _pin_from_addresses(host: str, addresses: list[str]) -> PinnedEndpoint | None:
    """Check every answer for `host` and return the one address to dial."""
    for address in addresses:
        if _is_blocked_address(address):
            raise BlockedDeliveryEndpointError(
                f"Delivery endpoint host {host!r} resolves to {address}, which is "
                "loopback/private/link-local and is not an allowed destination."
            )
    if not addresses:
        # Nothing resolved here, so there is nothing to pin and the dial falls
        # back to httpx's own lookup. Stating the residual case plainly rather
        # than implying it is closed: a zone that fails this lookup and answers
        # loopback for httpx's would still slip past. Refusing instead would
        # turn every transient resolver failure into a "blocked endpoint"
        # verdict for an endpoint that is not blocked, and would refuse names
        # this process cannot resolve but the runtime can (split-horizon DNS, a
        # container resolver). Operators who need that case closed set
        # REGENGINE_ALLOWED_DELIVERY_HOSTS, which gates on the name and never
        # depends on a resolver answering.
        return None
    # Every answer was checked; dial the first one. That is the address an
    # unpinned connect would have tried first anyway -- getaddrinfo returns
    # them in the platform's RFC 6724 preference order. The trade-off is that a
    # pinned dial does not fall through to the remaining answers if the first
    # is unreachable; a delivery endpoint whose primary address is down now
    # surfaces as a connection error instead of silently failing over.
    return PinnedEndpoint(hostname=host, address=addresses[0])


def assert_delivery_endpoint_allowed(endpoint: str) -> PinnedEndpoint | None:
    """Reject endpoints that are not permitted egress destinations.

    Raises BlockedDeliveryEndpointError (a ValueError) before any request is
    built, so no credential header is ever constructed for a blocked host.

    Synchronous entry point, for callers that are not already on an event loop.
    Coroutines must use `resolve_delivery_endpoint` instead: this one resolves
    with a blocking `socket.getaddrinfo`.

    Returns the single address this call resolved and approved, so the caller
    can dial that address instead of re-resolving the name (#207). Returns None
    when there is nothing to pin -- the host is already an IP literal, DNS
    checking is switched off, the private-host opt-out is in force, or the name
    did not resolve here. A None return is never a verdict of "unsafe": the
    endpoint has passed every check that applies to it.
    """
    host = _static_endpoint_checks(endpoint)
    if host is None:
        return None
    return _pin_from_addresses(host, _resolved_addresses(host))


async def resolve_delivery_endpoint(endpoint: str) -> PinnedEndpoint | None:
    """`assert_delivery_endpoint_allowed` for coroutines.

    Identical checks and identical verdicts; the resolver call is awaited
    instead of blocking the event loop.
    """
    host = _static_endpoint_checks(endpoint)
    if host is None:
        return None
    return _pin_from_addresses(host, await _aresolved_addresses(host))


# The genuine httpx client class, captured before any test double can replace
# the module attribute. A pinned dial is expressed through an httpx transport,
# so it can only be attached to a real httpx client; substituted clients (the
# in-memory doubles this repo's tests install over `httpx.AsyncClient`) never
# open a socket, so there is nothing for a pin to protect there.
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class _PinnedAddressTransport(httpx.AsyncHTTPTransport):
    """Dial the address the guard approved, under the original hostname.

    Rewriting the URL host to the literal is what stops httpx from resolving
    the name a second time. Everything name-shaped is preserved:

    * the `Host` header was already set from the original URL when the request
      was built (`httpx.Request._prepare`), so the server still sees the name;
    * `sni_hostname` carries the name into the TLS handshake, and httpx's
      default SSL context has `check_hostname=True`, so the certificate is
      verified against the hostname -- never against the pinned literal.

    Only the pinned host is rewritten. A redirect or health probe aimed at any
    other host goes out untouched.
    """

    def __init__(self, pin: PinnedEndpoint, **transport_kwargs: Any) -> None:
        # No transport_kwargs in production: the defaults are exactly what
        # `httpx.AsyncClient()` would have built, so pinning changes where the
        # socket goes and nothing about how the connection is made. The
        # parameter exists so a test can point verification at its own CA.
        super().__init__(**transport_kwargs)
        self._pin = pin

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = (request.url.host or "").strip().lower().rstrip(".")
        if host == self._pin.hostname:
            request.url = request.url.copy_with(host=self._pin.address)
            request.extensions = {
                **request.extensions,
                "sni_hostname": self._pin.hostname,
            }
        return await super().handle_async_request(request)


def _environment_proxy_applies(endpoint: str) -> bool:
    """True when httpx would send this URL through an environment proxy.

    Mirrors httpx's own mount matching rather than guessing, because being
    wrong either way is costly: a proxied request that we pin would break (see
    _dial_pin), and an unproxied request that we decline to pin would leave the
    rebinding gap open.
    """
    try:
        from httpx._utils import URLPattern, get_environment_proxies
    except ImportError:  # pragma: no cover - httpx internals moved
        return any(
            os.getenv(name, "").strip()
            for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        )
    url = httpx.URL(endpoint)
    mounts = sorted(
        ((URLPattern(pattern), proxy) for pattern, proxy in get_environment_proxies().items()),
        key=lambda item: item[0],
    )
    for pattern, proxy in mounts:
        if pattern.matches(url):
            return proxy is not None
    return False


async def _dial_pin(endpoint: str) -> PinnedEndpoint | None:
    """Validate `endpoint` and return the address to dial, if any.

    Pinning is suppressed when an environment proxy handles the request. That
    is not a loophole: with a proxy in play the client never resolves the name
    at all -- it sends `CONNECT <name>:<port>` and the proxy resolves -- so
    there is no second local lookup to poison, and the guard's own resolution
    does not describe the connection either way. Pinning anyway would be
    actively harmful: httpcore's tunnel connection ignores the `sni_hostname`
    extension and takes the TLS `server_hostname` from the request origin, so a
    pinned URL would verify the certificate against the IP and fail, and
    passing an explicit transport to httpx would disable the proxy mounts
    (`allow_env_proxies = trust_env and transport is None`) and silently route
    around the operator's proxy. The residual risk -- a rebinding zone answering
    the *proxy* -- belongs to the proxy, not to this process.
    """
    pin = await resolve_delivery_endpoint(endpoint)
    if pin is None or _environment_proxy_applies(endpoint):
        return None
    return pin


def _open_live_client(pin: PinnedEndpoint | None) -> Any:
    """An httpx client for one live exchange, dialing the pinned address."""
    timeout = _live_timeout_seconds()
    client_cls = httpx.AsyncClient
    if (
        pin is not None
        and isinstance(client_cls, type)
        and issubclass(client_cls, _HTTPX_ASYNC_CLIENT)
    ):
        return client_cls(timeout=timeout, transport=_PinnedAddressTransport(pin))
    return client_cls(timeout=timeout)


# RegEngine's IngestEvent declares `input_traceability_lot_codes` as a
# **top-level** field, and its canonical-event writer populates
# `kdes["input_lot_codes"]` (the thing transformation lineage rows are built
# from) only from that top-level field. The simulator's RegEngineEvent carries
# the value inside the free-form `kdes` dict, where RegEngine's ingest path
# never looks -- so a transformation used to be accepted while producing zero
# input->output lineage links. Pydantic ignores the unknown key, so nothing
# rejected and nothing mapped.
#
# Hoisting happens here, at serialization time, rather than on RegEngineEvent:
# the `kdes` copy stays exactly where it is (this repo's lineage view, EPCIS
# export, CSV importer and the mock all read it) and the wire body additionally
# carries the field where RegEngine reads it. Both sides then agree.
INPUT_LOT_CODES_FIELD = "input_traceability_lot_codes"


def _input_lot_codes(kdes: Any) -> list[str] | None:
    """Input lot codes from an event's KDEs, or None when it declares none.

    Accepts the list form the engine and CSV importer produce and the bare
    string form a hand-written payload may carry.
    """
    if not isinstance(kdes, dict):
        return None
    raw = kdes.get(INPUT_LOT_CODES_FIELD)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    codes = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    return codes or None


def build_wire_body(payload: IngestPayload) -> dict[str, Any]:
    """Serialize an IngestPayload into the exact dict sent to RegEngine.

    Identical to `payload.model_dump(mode="json")` except that an event whose
    KDEs declare input lot codes also carries them at the top level, where
    RegEngine's `IngestEvent` reads them. Events that declare none are left
    untouched rather than sent an explicit `null`: the field is optional and
    defaults to None on RegEngine, so omitting it keeps every non-transformation
    event's wire shape byte-for-byte what it was before.
    """
    body = payload.model_dump(mode="json")
    events = body.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            codes = _input_lot_codes(event.get("kdes"))
            if codes is not None:
                event[INPUT_LOT_CODES_FIELD] = codes
    return body


def _response_error_detail(response: httpx.Response) -> str:
    """RegEngine's error body as a short string, JSON or not.

    Never raises: a non-JSON body degrades to raw text, and an unreadable body
    degrades to an empty string.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - any decode failure falls back to text
        payload = None
    if payload is not None:
        if isinstance(payload, dict):
            for key in ("detail", "message", "error"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:1000]
        try:
            return json.dumps(payload, separators=(",", ":"))[:1000]
        except (TypeError, ValueError):
            pass
    try:
        return (response.text or "").strip()[:1000]
    except Exception:  # noqa: BLE001 - body already consumed/undecodable
        return ""


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
<<<<<<< HEAD
# The probe reads GET /api/v1/webhooks/recent, whose dependency chain is
# strictly weaker than the /ingest route it is meant to certify. Verified
# against RegEngine's tree, /ingest additionally carries:
#
#   * require_active_subscription  -- /recent has no subscription gate, so a
#     402 is genuinely unreachable here and is deliberately not in this table.
#   * require_permission("webhooks.ingest") -- /recent takes bare
#     get_ingestion_principal, so a read-only-scoped key passes this probe and
#     then 403s on every ingest.
#   * _verify_webhook_signature -- /recent is unsigned, so an HMAC mismatch is
#     invisible here. That one is covered separately by comparing posture
#     against /health's webhook_hmac_configured; see check_connection.
#
# Nothing in this table can therefore promise ingest will work. The 200 detail
# says what was actually proven and what was not.
_CONNECTION_VERDICTS: dict[int, tuple[str, str]] = {
    200: (
        "connected",
        "Authenticated read succeeded: the API key and tenant are valid for reads. "
        "This does not prove ingest will work — the read path does not check the "
        "subscription, the webhooks.ingest scope, or the webhook signature.",
    ),
    401: ("unauthorized", "RegEngine rejected the API key (HTTP 401). Check the key value and that it has not expired or been revoked."),
=======
# Scope caveat, deliberately reflected in the wording below: the probe is a
# GET on /api/v1/webhooks/recent, whose dependency chain is strictly weaker
# than POST /api/v1/webhooks/ingest. The read route carries neither the
# ingest route's active-subscription gate, nor its `webhooks.ingest`
# permission requirement, nor its HMAC signature verification, and it has
# its own rate-limit budget. A green read therefore proves the key and
# tenant are valid *for reads* and nothing more -- claiming it certifies
# ingestion is how a "connected" badge ends up sitting above a run in which
# every delivery 401s. The HMAC half of that gap is closed separately below
# by comparing signing posture across the two sides; subscription state and
# permission scope remain unobservable from this endpoint, so the 200 detail
# names them as unproven rather than implying otherwise.
_CONNECTION_VERDICTS: dict[int, tuple[str, str]] = {
    200: (
        "connected",
        "Authenticated read succeeded: the API key and tenant are valid for reads on this "
        "RegEngine workspace. This probe reads /api/v1/webhooks/recent, so it does not prove "
        "ingestion will succeed -- the subscription gate, the webhooks.ingest permission scope "
        "and the ingest rate limit apply to the write path only. Send a test batch to confirm "
        "end-to-end delivery.",
    ),
    401: ("unauthorized", "RegEngine rejected the API key (HTTP 401). Check the key value and that it has not expired or been revoked."),
    402: (
        "subscription_inactive",
        "RegEngine returned HTTP 402 for the read probe: the tenant's subscription is not "
        "active. Billing status must be active or trialing before ingestion resumes.",
    ),
>>>>>>> origin/main
    403: ("forbidden", "RegEngine refused the request (HTTP 403). The key may be missing the webhooks.read scope, or the tenant does not match the key."),
    404: ("tenant_mismatch", "RegEngine returned HTTP 404. Webhook reads return 404 when the tenant does not match the API key's tenant."),
    429: (
        "rate_limited",
<<<<<<< HEAD
        "RegEngine is rate limiting this tenant's webhook reads (HTTP 429). This is the "
        "read budget, which is a separate, smaller bucket from the ingest limit — ingest "
        "may still have headroom. Wait for the window shown in Retry-After and try again.",
=======
        "RegEngine is rate limiting this tenant's webhook reads (HTTP 429). Wait for the window "
        "shown in Retry-After and try again. Ingestion is metered separately, so this limit does "
        "not necessarily apply to delivery.",
>>>>>>> origin/main
    ),
    503: ("service_unavailable", "RegEngine is up but a required backing service is unavailable (HTTP 503). Try again shortly."),
}

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
        # Validate before any credential header is built, and keep the address
        # that validation approved so the dial cannot land anywhere else.
        pin = await _dial_pin(endpoint)

        idempotency_key = idempotency_key or uuid.uuid4().hex
        # Serialize the body exactly once so the bytes we sign are the same
        # bytes httpx puts on the wire. If we passed json=payload.model_dump()
        # to httpx, it would re-serialize and any whitespace/key-order drift
        # between our HMAC input and the wire body would cause RegEngine's
        # signature check to 401 on every request.
        #
        # `allow_nan=False` is not cosmetic: json.dumps defaults to emitting
        # the bare tokens NaN/Infinity/-Infinity, which are not JSON. RegEngine's
        # parser rejects them, so a non-finite quantity would be signed, sent,
        # and only then fail -- burning the idempotency key and reporting as a
        # remote error. Failing here raises ValueError before any credential
        # header or signature exists (#98).
        body_bytes = json.dumps(
            build_wire_body(payload),
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
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
        try:
            async with _open_live_client(pin) as client:
                response = await client.post(endpoint, headers=headers, content=body_bytes)
        except httpx.HTTPError as exc:
            # The transport never reached RegEngine. Logged here as well as
            # raised because the exception only ever reaches the one caller
            # holding the request; nothing else recorded that live ingest was
            # failing. Log the exception class rather than str(exc), which can
            # contain the full URL.
            logger.warning(
                "Live ingest transport error: endpoint=%s idempotency_key=%s events=%d error=%s",
                endpoint,
                idempotency_key,
                len(payload.events),
                exc.__class__.__name__,
            )
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc

        metadata = metadata | {"status_code": response.status_code}
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
<<<<<<< HEAD
            logger.warning(
                "Live ingest rejected: endpoint=%s idempotency_key=%s events=%d status=%s",
                endpoint,
                idempotency_key,
                len(payload.events),
                response.status_code,
            )
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc
=======
            # Keep RegEngine's own explanation (subscription status, rate-limit
            # window, key scope) instead of httpx's bare status-line text.
            detail = _response_error_detail(exc.response)
            if detail:
                metadata = metadata | {"error_body": detail}
            message = f"{exc}: {detail}" if detail else str(exc)
            raise LiveRegEngineDeliveryError(message, metadata) from exc
>>>>>>> origin/main
        return LiveIngestResult(response=response.json(), metadata=metadata)

    async def check_connection(self, config: SimulationConfig) -> ConnectionCheckResult:
        """Probe RegEngine with the configured credentials without ingesting.

        Uses GET /api/v1/webhooks/recent?limit=1 — the cheapest authenticated
        read on the webhook surface — and maps the response to the same
        failure vocabulary a live integrator sees (bad key, wrong tenant,
        lapsed subscription, rate limit).
        """
        endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
        api_key = config.delivery.api_key
        tenant_id = config.delivery.tenant_id
        parsed = urlparse(endpoint)
        host = parsed.netloc
        try:
            pin = await _dial_pin(endpoint)
        except BlockedDeliveryEndpointError as exc:
            # Refuse before credentials are placed in any header.
            return ConnectionCheckResult(
                verdict=BLOCKED_ENDPOINT_VERDICT,
                detail=str(exc),
                endpoint_host=host,
            )
        if not api_key or not tenant_id:
            # Name the field that is actually missing. Restating the rule
            # ("both are required") left an operator who had set one of the two
            # reading a message that described a condition they had already met.
            missing = [
                label
                for label, value in (("an API key", api_key), ("a tenant id", tenant_id))
                if not value
            ]
            return ConnectionCheckResult(
                verdict="not_configured",
                detail=f"Missing {' and '.join(missing)}. Both are required before testing the connection.",
                endpoint_host=host,
            )

        probe_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/webhooks/recent"
        headers = {
            "X-RegEngine-API-Key": api_key,
            "X-Tenant-ID": tenant_id,
        }
        try:
            async with _open_live_client(pin) as client:
                response = await client.get(
                    probe_url,
                    headers=headers,
                    params={"tenant_id": tenant_id, "limit": 1},
                )
                remote_health = await _fetch_remote_health(client, parsed)
        except httpx.HTTPError as exc:
            logger.warning(
                "Connection test could not reach RegEngine: host=%s error=%s",
                host,
                exc.__class__.__name__,
            )
            return ConnectionCheckResult(
                verdict="unreachable",
                detail=f"Could not reach RegEngine at {host}: {exc.__class__.__name__}",
                endpoint_host=host,
            )

        verdict, detail = _CONNECTION_VERDICTS.get(
            response.status_code,
            ("error", f"Unexpected HTTP {response.status_code} from RegEngine."),
        )
<<<<<<< HEAD
        remote_contract = remote_health.contract_version
=======
        # The read probe cannot see the ingest route's signature check, so
        # compare signing posture across the two sides instead. This is the one
        # ingest-only gate that is observable without writing, and the one most
        # likely to be half-configured in a shared demo.
        signature_detail = _signature_posture_detail(_remote_hmac_configured(remote_health))
        if verdict == "connected" and signature_detail is not None:
            return ConnectionCheckResult(
                verdict=SIGNATURE_MISMATCH_VERDICT,
                detail=signature_detail,
                status_code=response.status_code,
                endpoint_host=host,
            )
        remote_contract = _remote_contract_version(remote_health)
>>>>>>> origin/main
        if verdict == "connected" and remote_contract is not None and remote_contract != INFLOW_CONTRACT_VERSION:
            verdict = "contract_mismatch"
            detail = (
                f"Credentials are valid, but this simulator implements ingest contract "
                f"v{INFLOW_CONTRACT_VERSION} while RegEngine advertises v{remote_contract}. "
                "One side is running an older deploy — update it before ingesting, or "
                "payloads may validate here and be rejected there (or vice versa)."
            )
        elif verdict == "connected":
            # The probe reads an unsigned endpoint, so an HMAC mismatch cannot
            # show up in its status code -- it would show up later as a 401 on
            # every single ingest. Compare posture instead. Both directions
            # matter and they fail differently, so they are worded separately.
            signature_detail = _signature_posture_detail(remote_health.hmac_configured)
            if signature_detail is not None:
                verdict = "signature_mismatch"
                detail = signature_detail
        return ConnectionCheckResult(
            verdict=verdict,
            detail=detail,
            status_code=response.status_code,
            endpoint_host=host,
        )


<<<<<<< HEAD
def _signature_posture_detail(remote_hmac_configured: bool | None) -> str | None:
    """Compare local and remote webhook-signing posture, if both are known.

    Returns None when the postures agree, or when the remote does not advertise
    its posture (an older deploy) -- absence is tolerated rather than guessed
    at, the same way contract-version skew detection is.

    This exists because the connection probe reads an unsigned endpoint. A
    one-sided HMAC configuration is invisible to it and then fails 100% of
    ingests, which is the exact "green demo, failing live post" shape the probe
    is supposed to prevent.
    """
    if remote_hmac_configured is None:
        return None

    local_configured = bool(os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip())
    if local_configured == remote_hmac_configured:
        return None

    if remote_hmac_configured:
        return (
            "Reads succeed, but ingest will not: RegEngine requires signed webhooks and "
            f"{WEBHOOK_HMAC_SECRET_ENV} is not set here, so every ingest will be rejected "
            "with HTTP 401. Set the same secret on both sides."
        )
    return (
        f"Reads succeed, but {WEBHOOK_HMAC_SECRET_ENV} is set here while RegEngine reports "
        "webhook signing is off, so bodies will be accepted there without their signature "
        "being verified. Either enable signing on RegEngine or clear the local secret."
    )


@dataclass(frozen=True, slots=True)
class RemoteHealth:
    """What RegEngine's /health tells us about the far side of the wire."""

    contract_version: str | None = None
    # None when the deployment predates the field, which is distinct from
    # False ("HMAC is off there") -- posture comparison only engages when the
    # remote actually advertises.
    hmac_configured: bool | None = None


async def _fetch_remote_health(client: httpx.AsyncClient, parsed) -> RemoteHealth:
    """Best-effort read of RegEngine's advertised ingest posture.

    Returns empty fields when the health endpoint is unreachable, non-JSON
    (some deployments serve the frontend at /health), or predates a field —
    skew detection only engages when both sides advertise.
=======
async def _fetch_remote_health(client: httpx.AsyncClient, parsed) -> dict[str, Any] | None:
    """Best-effort read of RegEngine's /health document.

    Returns None when the health endpoint is unreachable or non-JSON (some
    deployments serve the frontend at /health). Every skew check built on it
    engages only when the remote actually advertises the field it needs, so an
    older RegEngine simply yields no extra verdict.
>>>>>>> origin/main
    """
    try:
        health = await client.get(f"{parsed.scheme}://{parsed.netloc}/health")
        payload = health.json()
    except (httpx.HTTPError, ValueError):
<<<<<<< HEAD
        return RemoteHealth()
    if not isinstance(payload, dict):
        return RemoteHealth()
    version = payload.get("inflow_contract_version")
    hmac_configured = payload.get("webhook_hmac_configured")
    return RemoteHealth(
        contract_version=str(version) if version is not None else None,
        hmac_configured=hmac_configured if isinstance(hmac_configured, bool) else None,
    )
=======
        return None
    return payload if isinstance(payload, dict) else None


def _remote_contract_version(health: dict[str, Any] | None) -> str | None:
    """RegEngine's advertised inflow contract version, when it advertises one."""
    if health is None:
        return None
    value = health.get("inflow_contract_version")
    return str(value) if value is not None else None
>>>>>>> origin/main


# Key names RegEngine's /health may use for its `webhook_hmac_configured()`
# posture flag. Only a real boolean (or its string spelling) counts -- a
# missing or unrecognized value leaves signing posture unknown, and unknown
# never produces a verdict.
_REMOTE_HMAC_KEYS = ("webhook_hmac_configured", "hmac_configured")


def _remote_hmac_configured(health: dict[str, Any] | None) -> bool | None:
    if health is None:
        return None
    for key in _REMOTE_HMAC_KEYS:
        value = health.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _TRUTHY:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
    return None


def _signature_posture_detail(remote_hmac_configured: bool | None) -> str | None:
    """Explain a webhook-signing mismatch between the two sides, if any.

    Returns None when the posture matches or when RegEngine does not advertise
    its posture at all — the probe never invents a failure it cannot see.
    """
    if remote_hmac_configured is None:
        return None
    local_configured = bool(os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip())
    if local_configured == remote_hmac_configured:
        return None
    if remote_hmac_configured:
        return (
            "Credentials are valid for reads, but RegEngine has webhook HMAC signing enabled "
            f"while {WEBHOOK_HMAC_SECRET_ENV} is unset here. Every live ingest would be sent "
            "unsigned and rejected with HTTP 401 — the read probe cannot see this because only "
            "the ingest route verifies signatures. Set the same secret on both sides."
        )
    return (
        f"Credentials are valid for reads, but {WEBHOOK_HMAC_SECRET_ENV} is set here while "
        "RegEngine reports webhook HMAC signing disabled. Ingest still succeeds and RegEngine's "
        "verifier no-ops, so bodies go unverified — set WEBHOOK_HMAC_SECRET on RegEngine, or "
        "clear the local secret, so both sides agree."
    )


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

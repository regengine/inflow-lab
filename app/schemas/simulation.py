from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from ..scenarios import ScenarioId
from .domain import DestinationMode, OperationScale


MockFrictionCode = Literal["invalid_key", "subscription_inactive", "rate_limit"]

# Must match the host in regengine_client.DEFAULT_LIVE_INGEST_ENDPOINT. Kept
# as its own constant (rather than imported) because regengine_client.py
# already imports from this module — importing back would be circular.
_TRUSTED_REGENGINE_HOST = "www.regengine.co"

# Escape hatch for local development against http://localhost:... or the
# built-in mock service. OFF by default: a deployment that forgets to set
# this still gets the full loopback/private/link-local/metadata block below.
# Set only in a trusted local/dev shell — never in a shared or production
# environment, since it disables the SSRF guard entirely.
PRIVATE_ENDPOINTS_ENV = "REGENGINE_ALLOW_PRIVATE_ENDPOINTS"

# Relaxes the scheme check only, for a trusted network where TLS terminates
# elsewhere. Private, loopback and metadata destinations stay refused, and
# PRIVATE_ENDPOINTS_ENV already implies this -- the local-mock workflow runs
# over http://localhost and needs nothing new.
ALLOW_CLEARTEXT_DELIVERY_ENV = "REGENGINE_ALLOW_CLEARTEXT_DELIVERY"

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
})

_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".internal",
    ".local",
)


class EgressBlockedError(ValueError):
    """A delivery endpoint failed the egress guard (see validate_egress_endpoint).

    Subclasses ValueError so the app-wide ValueError handler registered in
    main.py turns it into a clean 4xx rather than an unhandled 500 wherever
    it escapes. The /api/integration/test route also catches it explicitly,
    to answer a refused probe with a 400 that names the blocked host.
    """


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _private_endpoints_allowed() -> bool:
    return _env_flag(PRIVATE_ENDPOINTS_ENV)


def _cleartext_delivery_allowed() -> bool:
    # PRIVATE_ENDPOINTS_ENV implies this: that hatch exists for
    # http://localhost and the built-in mock, which are cleartext by nature.
    return _env_flag(ALLOW_CLEARTEXT_DELIVERY_ENV) or _private_endpoints_allowed()


def _resolved_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *host* to its IP addresses, cheaply.

    A bare getaddrinfo() call — no reverse lookups, no service-name lookup.
    IP literals resolve instantly with no network round-trip; only bare
    hostnames actually hit the resolver. Any failure (NXDOMAIN, an
    offline/sandboxed network, a malformed IDN host) comes back as no
    addresses, so the caller fails closed instead of guessing.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        raw_ip = str(info[4][0]).split("%", 1)[0]  # strip an IPv6 zone id, e.g. fe80::1%eth0
        try:
            resolved.append(ipaddress.ip_address(raw_ip))
        except ValueError:
            continue
    return resolved


def _is_unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, private, link-local (this is where 169.254.169.254 cloud
    metadata lives), reserved, unspecified, or multicast — the address
    classes an SSRF probe uses to reach the host itself, its LAN, or its
    cloud control plane instead of the open internet."""
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    if address.is_unspecified or address.is_reserved or address.is_multicast:
        return True
    # An IPv4-mapped IPv6 literal (::ffff:127.0.0.1) hides an unsafe v4
    # address inside a technically-"routable" v6 one — unwrap and re-check.
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and _is_unsafe_address(mapped)


def _as_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The host as an IP address if it is already a literal, else None.

    Purely syntactic -- never touches the resolver -- so it is safe to call
    ahead of checks that are deliberately resolver-free.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _unsafe_address_message(host: str) -> str:
    return (
        f"Delivery endpoint host {host!r} resolves to a loopback, private, "
        "link-local, or cloud-metadata address, which is blocked to prevent "
        f"SSRF. Set {PRIVATE_ENDPOINTS_ENV}=1 to allow this for local "
        "development."
    )


async def async_resolve_egress_endpoint(url: HttpUrl | None) -> str | None:
    """``resolve_egress_endpoint`` for callers running on the event loop.

    The guard's one expensive step is ``socket.getaddrinfo``, which is
    blocking C code: called straight from ``async def ingest`` /
    ``check_connection`` it would stall the whole event loop for the length
    of a DNS lookup on every live delivery. ``asyncio.to_thread`` moves it to
    the default executor.

    The helpers it calls are looked up through this module's globals on the
    worker thread, so monkeypatching them in a test works unchanged.
    """
    import asyncio
    return await asyncio.to_thread(resolve_egress_endpoint, url)


async def async_validate_egress_endpoint(url: HttpUrl | None) -> None:
    """Async wrapper that offloads the blocking DNS resolution to a thread.

    Discards the pinned address, exactly as the synchronous
    ``validate_egress_endpoint`` does -- see ``resolve_egress_endpoint``.
    """
    await async_resolve_egress_endpoint(url)


def validate_egress_endpoint(url: HttpUrl | None) -> None:
    """``resolve_egress_endpoint`` for callers that only need the verdict.

    Same check, same exception; the resolved address is discarded. Kept as a
    named entry point because most callers (and the DeliveryConfig-level
    tests) care only about "is this endpoint allowed", and because a caller
    that discards the address is a caller that is NOT pinning -- which is a
    property worth being able to see at the call site.
    """
    resolve_egress_endpoint(url)


def resolve_egress_endpoint(url: HttpUrl | None) -> str | None:
    """Reject a RegEngine delivery endpoint before it is ever dialed, and
    return the ONE address the caller must dial it at.

    Enforced at request time, from check_connection() and ingest() in
    regengine_client.py, immediately before the outbound call. Deliberately
    NOT a Pydantic field validator: resolving DNS during model construction
    would make building a DeliveryConfig depend on the network, so anywhere
    without a resolver every endpoint would fail closed at once; and a
    validated-at-construction address says nothing about the address dialed
    minutes later. Checking here covers that, and covers every route carrying
    an inline `delivery` block as well as the model_copy(update=...) path
    /api/integration/test and /api/integration/configure use.

    WHAT THE RETURN VALUE IS FOR (#207). This function used to resolve,
    check, and then throw the answer away -- httpx resolved the hostname a
    second time, independently, when it opened the socket. A hostile
    authoritative server with a zero/short TTL could answer the guard's
    lookup with a public address and httpx's with 127.0.0.1, and the request
    landed on loopback with the caller's credential attached. Returning the
    address closes that: the caller dials this exact address (see
    _pinned_dial in regengine_client.py), carrying the original hostname
    through as the Host header and the TLS SNI/verification name, so the
    address that was checked is the address that is connected to and the name
    is resolved exactly once.

    Returns None -- meaning "dial by hostname, there is nothing to pin" -- in
    the four cases where pinning is either impossible or not the guard's
    business, each of which still resolves at most once in total:

      * ``url is None``: no endpoint configured, so nothing to check or dial.
      * REGENGINE_ALLOW_PRIVATE_ENDPOINTS is set: the operator has explicitly
        opted out of this guard for local development, so it does not resolve
        at all. Pinning here would also cost the dual-stack fallback httpx
        gets for free from ``localhost`` (::1 then 127.0.0.1) and buy nothing
        -- loopback is the intended destination.
      * The documented RegEngine host: allowlisted by name and never resolved
        here, so httpx's lookup is the only one. Pinning our own first answer
        could not defend it anyway -- rebinding needs two lookups to disagree
        -- and forcing a lookup would tie every default-endpoint test and
        offline demo to a working resolver.
      * The host does not resolve: it cannot be dialed, so it is not a pivot.

    Otherwise every resolved address must be public: loopback, private,
    link-local, reserved, unspecified and multicast addresses are refused,
    and the first address is returned to be pinned. Only the first: a host
    with several A records loses httpx's connect-time failover across the
    rest, which is the deliberate price of pinning -- an address that was
    never validated must never be dialed, and "try the next one" is exactly
    the door rebinding walks through.

    Escape hatch: set REGENGINE_ALLOW_PRIVATE_ENDPOINTS=1 to allow loopback/
    private/link-local endpoints for local development against
    http://localhost:... or the built-in mock service. Off by default.
    """
    if url is None or _private_endpoints_allowed():
        return None
    if url.username or url.password:
        raise EgressBlockedError(
            "Delivery endpoint must not contain userinfo (user:pass@host). "
            "Use the api_key field instead."
        )
    host = (url.host or "").strip("[]").lower()
    if not host:
        raise EgressBlockedError("Delivery endpoint is missing a host.")
    # Name checks run before the trusted-host short-circuit and before the
    # scheme check, so a metadata or loopback destination is always reported
    # as the local/internal address it is -- that is the finding an operator
    # has to act on, even when the endpoint is also cleartext.
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(s) for s in _BLOCKED_HOST_SUFFIXES):
        raise EgressBlockedError(
            f"Delivery endpoint host {host!r} is blocked by name. "
            f"Set {PRIVATE_ENDPOINTS_ENV}=1 to allow this for local "
            "development."
        )
    # An address literal can be classified with no resolver at all, so the
    # local/internal verdict is reached before the scheme check even for a
    # cleartext metadata URL like http://169.254.169.254/. Names still wait
    # for the resolution below, so the scheme check stays resolver-free.
    literal = _as_ip_address(host)
    if literal is not None and _is_unsafe_address(literal):
        raise EgressBlockedError(_unsafe_address_message(host))
    if url.scheme == "http" and not _cleartext_delivery_allowed():
        # Every live delivery and every connection probe carries the RegEngine
        # API key in a request header. Over http that header crosses the
        # network in the clear, so an endpoint that is otherwise a perfectly
        # ordinary public host still hands the credential to anything on the
        # path. Refused here -- before the name is resolved and before any
        # credential header is built -- rather than at the socket.
        raise EgressBlockedError(
            f"Delivery endpoint {url.scheme}://{host} is cleartext, and the RegEngine "
            "API key would cross the network unencrypted in a request header. Use "
            f"https, or set {ALLOW_CLEARTEXT_DELIVERY_ENV}=1 for a trusted network."
        )
    if host == _TRUSTED_REGENGINE_HOST:
        return None
    addresses = _resolved_addresses(host)
    if not addresses:
        # A host that does not resolve cannot be dialed, so it is not an SSRF
        # vector: the connection attempt fails on its own a moment later.
        # Rejecting here would instead tie the guard to resolver availability,
        # so an offline or sandboxed environment would classify every endpoint
        # as hostile and fail all live delivery closed at once.
        return None
    if any(_is_unsafe_address(address) for address in addresses):
        raise EgressBlockedError(_unsafe_address_message(host))
    return str(addresses[0])


class DeliveryConfig(BaseModel):
    # extra="forbid" (#143): this arrives inline in request bodies (the
    # integration routes, and the `delivery` block on CSV-import, replay and
    # demo-fixture requests). A misspelled key here silently keeps the stored
    # default -- including leaving delivery on mock when the caller meant to
    # point it somewhere else -- so it must be a 422, not a quiet no-op.
    model_config = ConfigDict(extra="forbid")

    mode: DestinationMode = DestinationMode.MOCK
    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None
    # Failure-injection codes honored by mock delivery only, so operators can
    # rehearse the auth/billing/rate-limit friction a live integration hits.
    mock_friction: list[MockFrictionCode] = Field(default_factory=list)


class SimulationConfig(BaseModel):
    # extra="forbid" (#143): POST /api/simulate/reset validates the raw body
    # directly as this model, so a caller who wrapped it as {"config": {...}}
    # -- the shape /start takes -- got a 200 and a silently unchanged
    # scenario. There is no field named "config" here, so forbidding extras
    # turns that mistake into the 422 it always should have been.
    model_config = ConfigDict(extra="forbid")

    source: str = "codex-simulator"
    scenario: ScenarioId = ScenarioId.LEAFY_GREENS_SUPPLIER
    scale: OperationScale = OperationScale.MIDSIZE
    interval_seconds: float = 1.5
    batch_size: int = 3
    seed: int | None = 204
    persist_path: str = "data/events.jsonl"
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, value: float) -> float:
        if value < 0:
            raise ValueError("interval_seconds must be >= 0")
        return value

    @field_validator("batch_size")
    @classmethod
    def validate_batch_size(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("batch_size must be between 1 and 100")
        return value


class StartRequest(BaseModel):
    config: SimulationConfig


class StepRequest(BaseModel):
    config: SimulationConfig | None = None


class StatusResponse(BaseModel):
    running: bool
    config: SimulationConfig
    stats: dict[str, Any]


class StepResponse(BaseModel):
    generated: int
    posted: int
    accepted: int = 0
    rejected: int = 0
    failed: int
    lot_codes: list[str]
    delivery_status: Literal["generated", "posted", "failed"]
    delivery_mode: DestinationMode
    delivery_attempts: int
    response: dict[str, Any] | None = None
    error: str | None = None


class ResetResponse(BaseModel):
    status: str

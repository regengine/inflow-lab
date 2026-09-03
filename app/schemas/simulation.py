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


def _private_endpoints_allowed() -> bool:
    return os.getenv(PRIVATE_ENDPOINTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


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
        raw_ip = info[4][0].split("%", 1)[0]  # strip an IPv6 zone id, e.g. fe80::1%eth0
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


async def async_validate_egress_endpoint(url: HttpUrl | None) -> None:
    """Async wrapper that offloads the blocking DNS resolution to a thread."""
    import asyncio
    await asyncio.to_thread(validate_egress_endpoint, url)


def validate_egress_endpoint(url: HttpUrl | None) -> None:
    """Reject a RegEngine delivery endpoint before it is ever dialed.

    Enforced at request time, from check_connection() and ingest() in
    regengine_client.py, immediately before the outbound call. Deliberately
    NOT a Pydantic field validator: resolving DNS during model construction
    would make building a DeliveryConfig depend on the network, so anywhere
    without a resolver every endpoint would fail closed at once; and it would
    not stop DNS rebinding anyway, since the address can change between
    validation and the request. Guarding where the socket is actually opened
    covers both, and covers every route carrying an inline `delivery` block
    as well as the model_copy(update=...) path /api/integration/test and
    /api/integration/configure use.

    The documented RegEngine host is allowlisted outright, which is cheap
    and keeps this offline-safe for anything that already targets it. Every
    other host must resolve to public addresses only: loopback, private,
    link-local, reserved, unspecified and multicast addresses are refused. A
    host that does not resolve is allowed through to fail as an ordinary
    connection error, because it cannot be dialed and so cannot be a pivot.

    Escape hatch: set REGENGINE_ALLOW_PRIVATE_ENDPOINTS=1 to allow loopback/
    private/link-local endpoints for local development against
    http://localhost:... or the built-in mock service. Off by default.
    """
    if url is None or _private_endpoints_allowed():
        return
    host = (url.host or "").strip("[]").lower()
    if not host:
        raise EgressBlockedError("Delivery endpoint is missing a host.")
    if host == _TRUSTED_REGENGINE_HOST:
        return
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(s) for s in _BLOCKED_HOST_SUFFIXES):
        raise EgressBlockedError(
            f"Delivery endpoint host {host!r} is blocked by name. "
            f"Set {PRIVATE_ENDPOINTS_ENV}=1 to allow this for local "
            "development."
        )
    addresses = _resolved_addresses(host)
    if not addresses:
        # A host that does not resolve cannot be dialed, so it is not an SSRF
        # vector: the connection attempt fails on its own a moment later.
        # Rejecting here would instead tie the guard to resolver availability,
        # so an offline or sandboxed environment would classify every endpoint
        # as hostile and fail all live delivery closed at once.
        return
    if any(_is_unsafe_address(address) for address in addresses):
        raise EgressBlockedError(
            f"Delivery endpoint host {host!r} resolves to a loopback, private, "
            "link-local, or cloud-metadata address, which is blocked to prevent "
            f"SSRF. Set {PRIVATE_ENDPOINTS_ENV}=1 to allow this for local "
            "development."
        )


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

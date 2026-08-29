from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

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

# Optional strict egress allowlist, comma-separated. A leading dot matches
# subdomains (".regengine.co" allows "www.regengine.co"). Unset means any
# public host is permitted.
ALLOWED_DELIVERY_HOSTS_ENV = "REGENGINE_ALLOWED_DELIVERY_HOSTS"
# Opt back in to cleartext http delivery to a public host. Off by default.
ALLOW_CLEARTEXT_DELIVERY_ENV = "REGENGINE_ALLOW_CLEARTEXT_DELIVERY"


class EgressBlockedError(ValueError):
    """A delivery endpoint failed the egress guard (see validate_egress_endpoint).

    Subclasses ValueError so the app-wide ValueError handler registered in
    main.py turns it into a clean 4xx rather than an unhandled 500 wherever
    it escapes. The /api/integration/test route also catches it explicitly,
    to answer a refused probe with a 400 that names the blocked host.
    """


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


def _cleartext_delivery_allowed() -> bool:
    return os.getenv(ALLOW_CLEARTEXT_DELIVERY_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


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


def resolve_egress_endpoint(url: HttpUrl | None) -> str | None:
    """Reject a RegEngine delivery endpoint before it is ever dialed, and
    return the ONE address the caller must dial it at.

    Enforced at request time, from check_connection() and ingest() in
    regengine_client.py, immediately before the outbound call. Deliberately
    NOT a Pydantic field validator: resolving DNS during model construction
    would make building a DeliveryConfig depend on the network, so anywhere
    without a resolver every endpoint would fail closed at once. Checking at
    request time also covers every route carrying an inline `delivery` block,
    as well as the model_copy(update=...) path /api/integration/test and
    /api/integration/configure use.

    WHAT THE RETURN VALUE IS FOR (#207). This function used to resolve, check,
    and then throw the answer away -- httpx resolved the hostname a second
    time, independently, when it opened the socket. A hostile authoritative
    server with a zero/short TTL could answer the guard's lookup with a public
    address and httpx's with 127.0.0.1, and the request landed on loopback
    with the caller's credential attached. Returning the address closes that:
    the caller dials this exact address (see _pinned_dial in
    regengine_client.py), carrying the original hostname through as the Host
    header and the TLS SNI/verification name, so the address that was checked
    is the address that is connected to and there is only ever one lookup.

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
        could not defend it anyway -- rebinding needs two lookups to disagree,
        and forcing a lookup here would tie every default-endpoint test and
        offline demo to a working resolver.
      * The host does not resolve: it cannot be dialed, so it is not a pivot.
        Rejecting here would instead tie the guard to resolver availability,
        so an offline or sandboxed environment would classify every endpoint
        as hostile and fail all live delivery closed at once.

    Otherwise every resolved address must be public: loopback, private,
    link-local (this is where 169.254.169.254 cloud metadata lives),
    reserved, unspecified and multicast are refused, and the first address is
    returned to be pinned. Only the first: a host with several A records
    loses httpx's connect-time failover across the rest, which is the
    deliberate price of pinning -- an address that was never validated must
    never be dialed, and "try the next one" is exactly the door rebinding
    walks through.

    Escape hatch: set REGENGINE_ALLOW_PRIVATE_ENDPOINTS=1 to allow loopback/
    private/link-local endpoints for local development against
    http://localhost:... or the built-in mock service. Off by default.
    """
    if url is None:
        return None
    host = (url.host or "").strip("[]").lower()
    if not host:
        raise EgressBlockedError("Delivery endpoint is missing a host.")

    # Gates on the NAME, so it holds even when DNS fails or answers hostilely,
    # and it is checked before the private-endpoint opt-out: an operator who
    # pins egress to their own RegEngine host means it, and a local-dev flag
    # must not widen it.
    allowlist = _allowed_delivery_hosts()
    if allowlist and not _host_matches_allowlist(host, allowlist):
        raise EgressBlockedError(
            f"Delivery endpoint host {host!r} is not in {ALLOWED_DELIVERY_HOSTS_ENV}."
        )

    if _private_endpoints_allowed():
        return None

    if url.scheme == "http" and not _cleartext_delivery_allowed():
        # Ordered after the private-endpoint opt-out (a local stack on
        # http://localhost is the documented dev workflow) but before the
        # address checks, so a public host is refused for its scheme while a
        # metadata or loopback host below is still reported as what it is.
        #
        # Every live delivery and probe carries the API key in a header; over
        # http that header crosses the network in the clear.
        raise EgressBlockedError(
            f"Delivery endpoint host {host!r} is cleartext http. The API key "
            "would be sent unencrypted. Use https, or set "
            f"{ALLOW_CLEARTEXT_DELIVERY_ENV}=1 for a trusted network."
        )

    if host == _TRUSTED_REGENGINE_HOST:
        return None
    addresses = _resolved_addresses(host)
    if not addresses:
        return None
    if any(_is_unsafe_address(address) for address in addresses):
        raise EgressBlockedError(
            f"Delivery endpoint host {host!r} resolves to a loopback, private, "
            "link-local, or cloud-metadata address, which is blocked to prevent "
            f"SSRF. Set {PRIVATE_ENDPOINTS_ENV}=1 to allow this for local "
            "development."
        )
    return str(addresses[0])


def validate_egress_endpoint(url: HttpUrl | None) -> None:
    """``resolve_egress_endpoint`` for callers that only need the verdict.

    Same check, same exception; the resolved address is discarded. Kept as
    the named entry point because most callers (and the DeliveryConfig-level
    tests) care only about "is this endpoint allowed", and because a caller
    that discards the address is a caller that is NOT pinning -- which is a
    property worth being able to see at the call site.
    """
    resolve_egress_endpoint(url)


async def resolve_egress_endpoint_async(url: HttpUrl | None) -> str | None:
    """``resolve_egress_endpoint`` for callers running on the event loop.

    The guard's one expensive step is ``socket.getaddrinfo``, which is
    blocking C code: called straight from ``async def ingest`` /
    ``check_connection`` it pinned the whole event loop for the duration of
    a DNS lookup, so one slow or timing-out resolver stalled every other
    request in the process -- including the SSE stream the console renders
    from (#209). Offloaded to a worker thread the same way #136 moved store
    I/O off the loop (``asyncio.to_thread`` throughout app/controller.py).

    The synchronous function stays the single implementation and remains
    the entry point for synchronous callers and tests; this only changes
    which thread it runs on. Module-level lookups inside it (notably
    ``_resolved_addresses``) still resolve through this module's globals on
    the worker thread, so monkeypatching them in a test works unchanged.
    """
    return await asyncio.to_thread(resolve_egress_endpoint, url)


async def validate_egress_endpoint_async(url: HttpUrl | None) -> None:
    """``validate_egress_endpoint`` for callers running on the event loop.

    Discards the pinned address, exactly as the synchronous wrapper does.
    See ``resolve_egress_endpoint_async`` for why this runs off the loop.
    """
    await resolve_egress_endpoint_async(url)


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


# What /start and /reset each want their config sent as. Kept as one string
# so both misnesting errors below describe the pair the same way and cannot
# drift from each other (#143).
_CONFIG_SHAPE_GUIDANCE = (
    'POST /api/simulate/start takes its config wrapped -- {"config": {...}} -- because a '
    "config is required to start. POST /api/simulate/reset takes the same fields unwrapped "
    "at the top level, because its config is optional: an empty body resets without "
    "reconfiguring."
)


def _default_persist_path() -> str:
    """The default event log, derived from REGENGINE_DATA_DIR at call time.

    A literal "data/events.jsonl" default put the default scope's events
    outside a mounted volume on any deployment that sets REGENGINE_DATA_DIR,
    so they were lost on restart. Read here rather than imported from
    app.tenancy, which imports this module.

    Deliberately a default_factory, not a module-level constant: the tests and
    the tenancy layer both set REGENGINE_DATA_DIR after import.
    """
    return str(Path(os.getenv("REGENGINE_DATA_DIR", "data")) / "events.jsonl")


class SimulationConfig(BaseModel):
    # extra="forbid" (#143): POST /api/simulate/reset validates the raw body
    # directly as this model, so a caller who wrapped it as {"config": {...}}
    # -- the shape /start takes -- got a 200 and a silently unchanged
    # scenario. There is no field named "config" here, so forbidding extras
    # turns that mistake into the 422 it always should have been.
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _explain_a_wrapped_config(cls, data: Any) -> Any:
        """Answer the specific mistake, not just refuse it (#143).

        extra="forbid" already stops `{"config": {...}}` reaching /reset,
        but it stops it with pydantic's generic "Extra inputs are not
        permitted" -- which tells a caller their body is wrong and nothing
        about what right looks like. Since "config" can never be a legal key
        on this model, seeing one is unambiguous evidence of exactly which
        mistake was made, and the error can say so.

        This is the deliberate alternative to unifying the two endpoints'
        body shapes: the mistake becomes impossible to make silently AND
        self-correcting, without a breaking change to a documented endpoint
        that the console, three operator scripts and the test suite all
        call. See the API reference in README.md.

        Save files never reach this: ScenarioSaveSnapshot prunes unknown
        config keys before validating (app/schemas/scenarios.py).
        """
        if isinstance(data, dict) and "config" in data:
            raise ValueError(
                "'config' is not a field of SimulationConfig -- the config fields belong "
                f"at this level, not nested under a 'config' key. {_CONFIG_SHAPE_GUIDANCE}"
            )
        return data

    source: str = "codex-simulator"
    scenario: ScenarioId = ScenarioId.LEAFY_GREENS_SUPPLIER
    scale: OperationScale = OperationScale.MIDSIZE
    interval_seconds: float = 1.5
    batch_size: int = 3
    seed: int | None = 204
    persist_path: str = Field(default_factory=_default_persist_path)
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

    @model_validator(mode="before")
    @classmethod
    def _explain_an_unwrapped_config(cls, data: Any) -> Any:
        """The mirror image of SimulationConfig's check above (#143).

        A caller who sends /reset's unwrapped body to /start got "Field
        required" against `config` -- accurate, and useless if you do not
        already know the wrapper exists, since the fields you sent are
        simply not mentioned. Recognising that the body IS a config, just
        at the wrong depth, lets the error say what to do with it.
        """
        if isinstance(data, dict) and "config" not in data:
            misplaced = sorted(set(data) & set(SimulationConfig.model_fields))
            if misplaced:
                raise ValueError(
                    f"config field(s) {misplaced} were sent at the top level. "
                    f"{_CONFIG_SHAPE_GUIDANCE}"
                )
        return data


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

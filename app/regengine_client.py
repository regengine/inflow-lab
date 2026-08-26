from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
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
# Optional strict allowlist: comma-separated hosts. A leading dot matches
# subdomains (".regengine.co" allows "www.regengine.co"). When unset, any
# public host is allowed but private/loopback/link-local destinations are not.
ALLOWED_DELIVERY_HOSTS_ENV = "REGENGINE_ALLOWED_DELIVERY_HOSTS"
# Escape hatch for developers pointing the simulator at a RegEngine stack
# running on localhost (see scripts/customer_journey.py). Off by default.
ALLOW_PRIVATE_DELIVERY_ENV = "REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS"
# DNS resolution of non-literal hosts can be disabled in sealed environments.
DELIVERY_DNS_GUARD_ENV = "REGENGINE_DELIVERY_DNS_GUARD"

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
    """Best-effort DNS resolution.

    A host that does not resolve cannot be reached, so an unresolvable name is
    not a bypass -- it is left to fail as an ordinary connection error rather
    than being reported as a blocked endpoint.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return [info[4][0] for info in infos]


def assert_delivery_endpoint_allowed(endpoint: str) -> None:
    """Reject endpoints that are not permitted egress destinations.

    Raises BlockedDeliveryEndpointError (a ValueError) before any request is
    built, so no credential header is ever constructed for a blocked host.
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
        return

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
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        return  # Literal address already checked; nothing to resolve.

    if os.getenv(DELIVERY_DNS_GUARD_ENV, "1").strip().lower() not in _TRUTHY:
        return
    for address in _resolved_addresses(host):
        if _is_blocked_address(address):
            raise BlockedDeliveryEndpointError(
                f"Delivery endpoint host {host!r} resolves to {address}, which is "
                "loopback/private/link-local and is not an allowed destination."
            )


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
_CONNECTION_VERDICTS: dict[int, tuple[str, str]] = {
    200: ("connected", "Authenticated read succeeded. Credentials and tenant are valid."),
    401: ("unauthorized", "RegEngine rejected the API key (HTTP 401). Check the key value and that it has not expired or been revoked."),
    402: ("subscription_inactive", "The tenant's subscription is not active (HTTP 402). Billing status must be active or trialing before ingestion resumes."),
    403: ("forbidden", "RegEngine refused the request (HTTP 403). The key may be missing the webhooks.read scope, or the tenant does not match the key."),
    404: ("tenant_mismatch", "RegEngine returned HTTP 404. Webhook reads return 404 when the tenant does not match the API key's tenant."),
    429: ("rate_limited", "RegEngine is rate limiting this tenant (HTTP 429). Wait for the window shown in Retry-After and try again."),
    503: ("service_unavailable", "RegEngine is up but a required backing service is unavailable (HTTP 503). Try again shortly."),
}


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
        # Validate before any credential header is built.
        assert_delivery_endpoint_allowed(endpoint)

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
        try:
            async with httpx.AsyncClient(timeout=_live_timeout_seconds()) as client:
                response = await client.post(endpoint, headers=headers, content=body_bytes)
        except httpx.HTTPError as exc:
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc

        metadata = metadata | {"status_code": response.status_code}
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Keep RegEngine's own explanation (subscription status, rate-limit
            # window, key scope) instead of httpx's bare status-line text.
            detail = _response_error_detail(exc.response)
            if detail:
                metadata = metadata | {"error_body": detail}
            message = f"{exc}: {detail}" if detail else str(exc)
            raise LiveRegEngineDeliveryError(message, metadata) from exc
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
            assert_delivery_endpoint_allowed(endpoint)
        except BlockedDeliveryEndpointError as exc:
            # Refuse before credentials are placed in any header.
            return ConnectionCheckResult(
                verdict=BLOCKED_ENDPOINT_VERDICT,
                detail=str(exc),
                endpoint_host=host,
            )
        if not api_key or not tenant_id:
            return ConnectionCheckResult(
                verdict="not_configured",
                detail="Both an API key and a tenant id are required before testing the connection.",
                endpoint_host=host,
            )

        probe_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/webhooks/recent"
        headers = {
            "X-RegEngine-API-Key": api_key,
            "X-Tenant-ID": tenant_id,
        }
        try:
            async with httpx.AsyncClient(timeout=_live_timeout_seconds()) as client:
                response = await client.get(
                    probe_url,
                    headers=headers,
                    params={"tenant_id": tenant_id, "limit": 1},
                )
                remote_contract = await _fetch_remote_contract_version(client, parsed)
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
        if verdict == "connected" and remote_contract is not None and remote_contract != INFLOW_CONTRACT_VERSION:
            verdict = "contract_mismatch"
            detail = (
                f"Credentials are valid, but this simulator implements ingest contract "
                f"v{INFLOW_CONTRACT_VERSION} while RegEngine advertises v{remote_contract}. "
                "One side is running an older deploy — update it before ingesting, or "
                "payloads may validate here and be rejected there (or vice versa)."
            )
        return ConnectionCheckResult(
            verdict=verdict,
            detail=detail,
            status_code=response.status_code,
            endpoint_host=host,
        )


async def _fetch_remote_contract_version(client: httpx.AsyncClient, parsed) -> str | None:
    """Best-effort read of RegEngine's advertised inflow contract version.

    Returns None when the health endpoint is unreachable, non-JSON (some
    deployments serve the frontend at /health), or predates the version
    field — skew detection only engages when both sides advertise.
    """
    try:
        health = await client.get(f"{parsed.scheme}://{parsed.netloc}/health")
        payload = health.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("inflow_contract_version")
    return str(value) if value is not None else None


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

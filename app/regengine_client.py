from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
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
    403: ("forbidden", "RegEngine refused the request (HTTP 403). The key may be missing the webhooks.read scope, or the tenant does not match the key."),
    404: ("tenant_mismatch", "RegEngine returned HTTP 404. Webhook reads return 404 when the tenant does not match the API key's tenant."),
    429: (
        "rate_limited",
        "RegEngine is rate limiting this tenant's webhook reads (HTTP 429). This is the "
        "read budget, which is a separate, smaller bucket from the ingest limit — ingest "
        "may still have headroom. Wait for the window shown in Retry-After and try again.",
    ),
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
            logger.warning(
                "Live ingest rejected: endpoint=%s idempotency_key=%s events=%d status=%s",
                endpoint,
                idempotency_key,
                len(payload.events),
                response.status_code,
            )
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc
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
            async with httpx.AsyncClient(timeout=_live_timeout_seconds()) as client:
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
        remote_contract = remote_health.contract_version
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
    """
    try:
        health = await client.get(f"{parsed.scheme}://{parsed.netloc}/health")
        payload = health.json()
    except (httpx.HTTPError, ValueError):
        return RemoteHealth()
    if not isinstance(payload, dict):
        return RemoteHealth()
    version = payload.get("inflow_contract_version")
    hmac_configured = payload.get("webhook_hmac_configured")
    return RemoteHealth(
        contract_version=str(version) if version is not None else None,
        hmac_configured=hmac_configured if isinstance(hmac_configured, bool) else None,
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

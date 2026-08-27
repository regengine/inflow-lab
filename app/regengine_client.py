from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .contract import INFLOW_CONTRACT_VERSION
from .schemas.ingestion import IngestPayload
from .schemas.simulation import SimulationConfig, validate_egress_endpoint
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
        # SSRF/credential-exfiltration guard — see validate_egress_endpoint's
        # docstring. Checked here, immediately before the request, so it sees
        # the endpoint every caller path actually ends up using. Note it does
        # NOT see the address httpx dials: httpx resolves the hostname again
        # on its own, so this stops a statically hostile endpoint but not DNS
        # rebinding. The docstring explains what closing that would take.
        validate_egress_endpoint(config.delivery.endpoint)

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
            return ConnectionCheckResult(
                verdict="not_configured",
                detail="Both an API key and a tenant id are required before testing the connection.",
                endpoint_host=host,
            )
        # SSRF/credential-exfiltration guard — see validate_egress_endpoint's
        # docstring. Checked immediately before the probe goes out. As in
        # ingest() above, the address checked is NOT guaranteed to be the
        # address dialed -- httpx resolves independently -- so this stops a
        # statically hostile endpoint but not DNS rebinding. The caller (the
        # /test route) turns a raised EgressBlockedError into a clean 4xx
        # rather than letting it become an unhandled 500.
        validate_egress_endpoint(config.delivery.endpoint)

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

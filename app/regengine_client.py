from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .models import IngestPayload, SimulationConfig


DEFAULT_LIVE_INGEST_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"


@dataclass(frozen=True, slots=True)
class LiveIngestResult:
    response: dict[str, Any]
    metadata: dict[str, Any]


class LiveRegEngineDeliveryError(RuntimeError):
    def __init__(self, message: str, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.metadata = metadata


class LiveRegEngineClient:
    async def ingest(self, payload: IngestPayload, config: SimulationConfig) -> LiveIngestResult:
        endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
        api_key = config.delivery.api_key
        tenant_id = config.delivery.tenant_id
        if not api_key or not tenant_id:
            raise ValueError("Live delivery requires both api_key and tenant_id")

        idempotency_key = uuid.uuid4().hex
        metadata = _delivery_metadata(endpoint=endpoint, idempotency_key=idempotency_key)
        headers = {
            "Content-Type": "application/json",
            "X-RegEngine-API-Key": api_key,
            "X-Tenant-ID": tenant_id,
            "Idempotency-Key": idempotency_key,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(endpoint, headers=headers, json=payload.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc

        metadata = metadata | {"status_code": response.status_code}
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LiveRegEngineDeliveryError(str(exc), metadata) from exc
        return LiveIngestResult(response=response.json(), metadata=metadata)


def _delivery_metadata(endpoint: str, idempotency_key: str) -> dict[str, Any]:
    parsed = urlparse(endpoint)
    return {
        "delivery_mode": "live",
        "endpoint_host": parsed.netloc,
        "endpoint_path": parsed.path or "/",
        "idempotency_key": idempotency_key,
        "status_code": None,
    }

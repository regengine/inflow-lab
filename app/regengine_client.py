from __future__ import annotations

import uuid
from typing import Any

import httpx

from .models import IngestPayload, SimulationConfig


DEFAULT_LIVE_INGEST_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"


class LiveRegEngineClient:
    async def ingest(self, payload: IngestPayload, config: SimulationConfig) -> dict[str, Any]:
        endpoint = str(config.delivery.endpoint) if config.delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
        api_key = config.delivery.api_key
        tenant_id = config.delivery.tenant_id
        if not api_key or not tenant_id:
            raise ValueError("Live delivery requires both api_key and tenant_id")

        headers = {
            "Content-Type": "application/json",
            "X-RegEngine-API-Key": api_key,
            "X-Tenant-ID": tenant_id,
            "Idempotency-Key": uuid.uuid4().hex,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(endpoint, headers=headers, json=payload.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()

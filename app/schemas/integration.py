from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl

from ..regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT
from .domain import DestinationMode
from .simulation import STRICT_REQUEST, MockFrictionCode


class IntegrationStatusResponse(BaseModel):
    """Sanitized view of the RegEngine connection — never includes secrets."""

    mode: DestinationMode
    endpoint: str
    endpoint_host: str
    # The endpoint live mode falls back to when none is configured. Served
    # so the console can show and use the backend's own default instead of
    # keeping a second copy of the URL in JavaScript: changing
    # `regengine_client.DEFAULT_LIVE_INGEST_ENDPOINT` changes what every
    # client sees, with no frontend edit.
    default_endpoint: str = DEFAULT_LIVE_INGEST_ENDPOINT
    api_key_configured: bool
    tenant_configured: bool
    hmac_configured: bool
    contract_version: str
    mock_friction: list[MockFrictionCode] = Field(default_factory=list)


class IntegrationConfigureRequest(BaseModel):
    """Partial update: omitted fields keep their current values, so the
    settings page can toggle mode without re-entering the API key."""

    model_config = STRICT_REQUEST

    mode: DestinationMode | None = None
    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None
    mock_friction: list[MockFrictionCode] | None = None


class ConnectionTestRequest(BaseModel):
    """Optional overrides so credentials can be tested before saving."""

    model_config = STRICT_REQUEST

    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None


class ConnectionTestResponse(BaseModel):
    verdict: str
    detail: str
    status_code: int | None = None
    endpoint_host: str = ""
    mode: DestinationMode

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .domain import DestinationMode
from .simulation import MockFrictionCode


class IntegrationStatusResponse(BaseModel):
    """Sanitized view of the RegEngine connection — never includes secrets."""

    mode: DestinationMode
    endpoint: str
    endpoint_host: str
    api_key_configured: bool
    tenant_configured: bool
    hmac_configured: bool
    contract_version: str
    mock_friction: list[MockFrictionCode] = Field(default_factory=list)


class IntegrationConfigureRequest(BaseModel):
    """Partial update: omitted fields keep their current values, so the
    settings page can toggle mode without re-entering the API key."""

    model_config = ConfigDict(extra="forbid")

    mode: DestinationMode | None = None
    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None
    mock_friction: list[MockFrictionCode] | None = None


class ConnectionTestRequest(BaseModel):
    """Optional overrides so credentials can be tested before saving."""

    model_config = ConfigDict(extra="forbid")

    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None


class ConnectionTestResponse(BaseModel):
    verdict: str
    detail: str
    status_code: int | None = None
    endpoint_host: str = ""
    mode: DestinationMode

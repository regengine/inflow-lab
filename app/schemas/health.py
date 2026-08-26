from __future__ import annotations

from pydantic import BaseModel

from .simulation import StatusResponse


class BuildInfoResponse(BaseModel):
    """Deploy provenance, as reported by ``build_info.public_dict()``."""

    version: str
    commit_sha: str | None = None
    commit_sha_short: str | None = None
    commit_source: str | None = None
    branch: str | None = None
    branch_source: str | None = None
    deployment_id: str | None = None
    deployment_source: str | None = None


class StoreProbeResponse(BaseModel):
    """Result of the event store's non-destructive write probe."""

    ok: bool
    persist_path: str
    error: str | None = None


class HealthAuthResponse(BaseModel):
    """Auth posture of the request that asked — never a credential."""

    enabled: bool
    username: str | None = None
    uses_default_storage: bool = True


class HealthzResponse(BaseModel):
    """Liveness/readiness probe body. HTTP 503 when ``ok`` is false."""

    ok: bool
    utc_time: str
    build: BuildInfoResponse
    contract_version: str
    store: StoreProbeResponse


class HealthResponse(HealthzResponse):
    """Tenant-aware health, adding who is asking and what the sim is doing."""

    tenant: str
    auth: HealthAuthResponse
    status: StatusResponse

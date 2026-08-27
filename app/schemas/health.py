"""Response models for the health endpoints (#146).

These routes returned bare ``dict[str, Any]``, so OpenAPI documented them as
``additionalProperties: true`` -- a consumer generating a client got no field
names at all. Adding the 503 branch then made it worse: the union return type
forced ``response_model=None``, and the schema collapsed to ``{}``.

Concrete models restore what #146 asked for: a caller reading the OpenAPI
document can see exactly which fields a health response carries, in both the
healthy and unhealthy cases.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BuildInfoResponse(BaseModel):
    """Mirrors build_info.BuildInfo.public_dict() exactly."""

    version: str | None = None
    commit_sha: str | None = None
    commit_sha_short: str | None = None
    commit_source: str | None = None
    branch: str | None = None
    branch_source: str | None = None
    deployment_id: str | None = None
    deployment_source: str | None = None


class HealthAuthResponse(BaseModel):
    enabled: bool
    username: str | None = None
    uses_default_storage: bool


class HealthzResponse(BaseModel):
    """The 200 body of the unauthenticated liveness probe, /api/healthz."""

    ok: bool
    utc_time: str
    build: BuildInfoResponse
    contract_version: str


class HealthzUnavailableResponse(HealthzResponse):
    """The 503 body: the event store cannot be written.

    A separate model rather than an optional `error` field on the healthy one.
    Making it optional would either serialize `"error": null` on every healthy
    response -- changing a payload these probes have always omitted it from --
    or need response_model_exclude_none, which would also drop
    `auth.username` when no user is authenticated, where null is the
    meaningful answer rather than an absence.
    """

    error: str


class HealthResponse(HealthzResponse):
    """The 200 body of the authenticated, tenant-scoped /api/health.

    ``status`` stays loosely typed on purpose -- it is
    ``SimulationController.status()``, which nests the whole sanitized config
    plus the store's stats and audit summary. Pinning its shape here would
    duplicate three other models and go stale the moment any of them changes;
    the fields #146 asked to see are this envelope's own.
    """

    tenant: str
    auth: HealthAuthResponse
    status: dict[str, Any]


class HealthUnavailableResponse(HealthzResponse):
    """The 503 body of /api/health.

    Carries no ``auth`` or ``status``: when the store cannot be written,
    ``controller.status()`` cannot be computed either, and reporting a stale
    or partial one would be worse than omitting it.
    """

    tenant: str
    error: str

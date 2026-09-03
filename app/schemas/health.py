from __future__ import annotations

from typing import Any

<<<<<<< HEAD
from pydantic import BaseModel, ConfigDict, Field

from .simulation import StatusResponse


class BuildInfoResponse(BaseModel):
    """The subset of build provenance the app is willing to publish.

    Mirrors ``app.build_info.BuildInfo.public_dict``; every field is optional
    because the deploy environment may supply none of them.
    """

    version: str | None = None
=======
from pydantic import BaseModel


class BuildInfoResponse(BaseModel):
    version: str
>>>>>>> origin/main
    commit_sha: str | None = None
    commit_sha_short: str | None = None
    commit_source: str | None = None
    branch: str | None = None
    branch_source: str | None = None
    deployment_id: str | None = None
    deployment_source: str | None = None


<<<<<<< HEAD
class HealthAuthResponse(BaseModel):
=======
class AuthInfoResponse(BaseModel):
>>>>>>> origin/main
    enabled: bool
    username: str | None = None
    uses_default_storage: bool


<<<<<<< HEAD
class HealthResponse(BaseModel):
    """``GET /api/health`` — the authenticated, tenant-scoped health view."""

    ok: bool
    utc_time: str
    build: BuildInfoResponse
    contract_version: str
    tenant: str
    auth: HealthAuthResponse
    status: StatusResponse


class HealthzResponse(BaseModel):
    """``GET /api/healthz`` — the unauthenticated liveness probe.

    ``railway.json`` points ``healthcheckPath`` here, so this shape is part of
    the deploy contract and not merely documentation.
    """

=======
class StoreProbeResponse(BaseModel):
    ok: bool
    persist_path: str
    error: str | None = None


class HealthzResponse(BaseModel):
>>>>>>> origin/main
    ok: bool
    utc_time: str
    build: BuildInfoResponse
    contract_version: str
<<<<<<< HEAD


class EPCISBody(BaseModel):
    """The ``epcisBody`` wrapper of an EPCIS 2.0 JSON-LD document.

    Individual events are left as free-form objects: their shape is decided by
    the CTE type being rendered (ObjectEvent vs TransformationEvent, each with
    its own required members), so pinning one model here would misdescribe the
    other. The document envelope around them is fully specified.
    """

    eventList: list[dict[str, Any]]


class EPCISDocumentResponse(BaseModel):
    """``GET /api/mock/regengine/export/epcis``.

    Field names are GS1's, so they intentionally do not follow the snake_case
    convention used by the rest of this API.
    """

    context: list[str] = Field(default_factory=list, alias="@context")
=======
    store: StoreProbeResponse


class HealthResponse(HealthzResponse):
    tenant: str
    auth: AuthInfoResponse
    status: dict[str, Any]


class EpcisEventBodyResponse(BaseModel):
    eventList: list[dict[str, Any]]


class EpcisDocumentResponse(BaseModel):
    """Top-level EPCIS 2.0 JSON-LD document.

    The ``@context`` field is serialised as-is in the real response; it is
    described here as ``dict | list`` because JSON-LD allows both shapes.
    """

>>>>>>> origin/main
    type: str
    schemaVersion: str
    creationDate: str
    sender: str
<<<<<<< HEAD
    epcisBody: EPCISBody

    model_config = ConfigDict(populate_by_name=True)
=======
    epcisBody: EpcisEventBodyResponse
>>>>>>> origin/main

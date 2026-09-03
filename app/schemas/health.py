from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BuildInfoResponse(BaseModel):
    version: str
    commit_sha: str | None = None
    commit_sha_short: str | None = None
    commit_source: str | None = None
    branch: str | None = None
    branch_source: str | None = None
    deployment_id: str | None = None
    deployment_source: str | None = None


class AuthInfoResponse(BaseModel):
    enabled: bool
    username: str | None = None
    uses_default_storage: bool


class HealthzResponse(BaseModel):
    ok: bool
    utc_time: str
    build: BuildInfoResponse
    contract_version: str


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

    type: str
    schemaVersion: str
    creationDate: str
    sender: str
    epcisBody: EpcisEventBodyResponse

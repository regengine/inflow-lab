from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .domain import FDAExportPreset, LineageEdge, LineageNode, StoredEventRecord


class FDAExportPresetSummary(BaseModel):
    id: FDAExportPreset
    label: str
    description: str
    requires_lot_code: bool = False


class FDAExportPresetListResponse(BaseModel):
    presets: list[FDAExportPresetSummary]


class LineageResponse(BaseModel):
    traceability_lot_code: str
    records: list[StoredEventRecord]
    nodes: list[LineageNode]
    edges: list[LineageEdge]


class EventListResponse(BaseModel):
    events: list[StoredEventRecord]


class EPCISDocumentResponse(BaseModel):
    """The EPCIS 2.0 JSON-LD document /export/epcis returns (#146).

    The route hands back a bare ``JSONResponse``, so OpenAPI documented its
    body as literally ``{}`` -- a consumer generating a client learned
    nothing at all about a regulatory export format. This publishes the
    envelope's own keys.

    Documentation only: the route still builds and returns the document
    through ``render_epcis_document``, and this is attached via ``responses=``
    rather than ``response_model=`` so nothing re-validates or re-serializes
    a payload whose shape GS1 owns. ``eventList`` entries stay loose for the
    same reason -- their shape varies by EPCIS event type, and pinning it
    here would duplicate ``app/epcis_export.py`` and go stale against it.
    """

    model_config = ConfigDict(populate_by_name=True)

    # "@context" and "type" are GS1's spellings, neither of which is a usable
    # Python identifier or a safe attribute name, so both carry an alias.
    context: list[Any] = Field(alias="@context")
    document_type: str = Field(alias="type")
    schemaVersion: str  # noqa: N815 -- GS1's own camelCase key
    creationDate: str  # noqa: N815 -- GS1's own camelCase key
    sender: str
    epcisBody: dict[str, Any]  # noqa: N815 -- GS1's own camelCase key

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
    """A lot trace, bounded and honest about it.

    ``records``/``nodes``/``edges`` describe only the records actually
    returned. ``total_records`` is how many the trace matched before the
    limit was applied, so ``truncated`` tells a client whether it is
    looking at the whole graph or the first ``limit`` records of it.
    """

    traceability_lot_code: str
    records: list[StoredEventRecord]
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    total_records: int = 0
    returned_records: int = 0
    limit: int | None = None
    truncated: bool = False


class EpcisDocumentResponse(BaseModel):
    """Documented shape of the EPCIS 2.0 JSON-LD export.

    The export route builds the document as a plain dict and returns it in
    a ``JSONResponse``; this model exists so the generated OpenAPI schema
    names the top-level JSON-LD envelope instead of shipping an empty
    ``{}``. Extra members are allowed on purpose — an EPCIS document is a
    JSON-LD document and may legitimately carry vocabulary this model does
    not enumerate.
    """

    model_config = ConfigDict(extra="allow")

    context: list[Any] | dict[str, Any] | str | None = Field(default=None, alias="@context")
    type: str | None = Field(default=None, alias="type")
    schemaVersion: str | None = None
    creationDate: str | None = None
    epcisBody: dict[str, Any] | None = None


class EventListResponse(BaseModel):
    events: list[StoredEventRecord]

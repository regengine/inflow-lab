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


class ExportBoundsSummary(BaseModel):
    """How a compliance export was bounded, mirroring ``LineageResponse``.

    Carried inside the EPCIS document (and alongside it in the
    ``X-Export-*`` response headers) so that a partial export can never be
    mistaken for a complete record set.
    """

    total_records: int = 0
    returned_records: int = 0
    limit: int | None = None
    truncated: bool = False
    warning: str | None = Field(
        default=None,
        description="Human-readable truncation notice; set only when truncated.",
    )


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
    sender: str | None = None
    epcisBody: dict[str, Any] | None = None
    export_summary: ExportBoundsSummary | None = Field(
        default=None,
        alias="regengine:exportSummary",
        description=(
            "How the export was bounded. Always present on documents served by "
            "/export/epcis; `truncated` true means events are missing from this "
            "document."
        ),
    )


class EventListResponse(BaseModel):
    events: list[StoredEventRecord]

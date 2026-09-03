from __future__ import annotations

from pydantic import BaseModel

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

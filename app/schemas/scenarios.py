from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..scenarios import ScenarioId
from .domain import DemoFixtureId, DestinationMode, StoredEventRecord
from .simulation import DeliveryConfig, SimulationConfig


class ScenarioSummary(BaseModel):
    id: ScenarioId
    label: str
    description: str
    industry_type: str
    operation_type: str
    reference_format: str
    requires_cooling: bool


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioSummary]


class ScenarioSaveSnapshot(BaseModel):
    scenario: ScenarioId
    config: SimulationConfig
    records: list[StoredEventRecord] = Field(default_factory=list)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScenarioSaveSummary(BaseModel):
    scenario: ScenarioId
    label: str
    saved_at: datetime
    record_count: int
    lot_codes: list[str]
    source: str
    persist_path: str
    delivery_mode: DestinationMode


class ScenarioSaveListResponse(BaseModel):
    saves: list[ScenarioSaveSummary]


class ScenarioSaveRequest(BaseModel):
    config: SimulationConfig | None = None


class ScenarioSaveResponse(BaseModel):
    status: Literal["saved"]
    save: ScenarioSaveSummary
    config: SimulationConfig


class ScenarioLoadResponse(BaseModel):
    status: Literal["loaded"]
    save: ScenarioSaveSummary
    config: SimulationConfig
    loaded_records: int


class DemoFixtureSummary(BaseModel):
    id: DemoFixtureId
    label: str
    description: str
    scenario: ScenarioId
    event_count: int
    lot_codes: list[str]


class DemoFixtureListResponse(BaseModel):
    fixtures: list[DemoFixtureSummary]


class DemoFixtureLoadRequest(BaseModel):
    reset: bool = True
    source: str | None = None
    delivery: DeliveryConfig | None = None


class DemoFixtureLoadResponse(BaseModel):
    status: Literal["loaded", "delivery_failed"]
    fixture_id: DemoFixtureId
    scenario: ScenarioId
    loaded: int
    stored: int
    posted: int
    failed: int
    source: str
    delivery_mode: DestinationMode
    delivery_attempts: int = 0
    lot_codes: list[str]
    response: dict[str, Any] | None = None
    error: str | None = None

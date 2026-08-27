from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # NOT forbidding extras here on purpose (contrast with the request
    # models below, #143). This is a storage model: ScenarioSaveStore
    # (app/scenario_saves.py) round-trips it verbatim to/from a JSON file
    # per scenario via model_dump_json()/model_validate_json(). Forbidding
    # extras would turn loading a save file written by an older or newer
    # version of this schema -- one with a field this version doesn't know
    # about -- into a hard failure instead of a harmless ignore.
    scenario: ScenarioId
    config: SimulationConfig
    records: list[StoredEventRecord] = Field(default_factory=list)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown_config_keys(cls, data: Any) -> Any:
        """Extend this model's forward-compatibility to its nested config.

        Being permissive at the snapshot level alone was not enough, and the
        gap contradicted the comment above. `SimulationConfig` and
        `DeliveryConfig` set extra="forbid" -- correctly, for the request
        boundary (#143) -- but they are also the version-carrying parts of a
        save file. So a save written by a version that added one config field
        did not "harmlessly ignore" it: the whole file failed to load, and
        because ScenarioSaveStore.list() calls get() for every id, one such
        file hid every other save behind a 400.

        Unknown keys are pruned here, on the way in from disk only. Request
        bodies are validated through SimulationConfig directly and never reach
        this validator, so a misspelled field in a request is still the 422
        that #143 made it.
        """
        if not isinstance(data, dict):
            return data
        config = data.get("config")
        if not isinstance(config, dict):
            return data

        pruned = {key: value for key, value in config.items() if key in SimulationConfig.model_fields}
        delivery = pruned.get("delivery")
        if isinstance(delivery, dict):
            pruned["delivery"] = {
                key: value for key, value in delivery.items() if key in DeliveryConfig.model_fields
            }
        if pruned != config:
            data = {**data, "config": pruned}
        return data


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
    # extra="forbid" (#143): request body for POST /api/scenario-saves/{id}.
    # Same failure mode as the /reset bug this issue was filed over -- a
    # misnamed wrapper key (e.g. "configuration" instead of "config") would
    # otherwise silently save hard-coded defaults with no error.
    model_config = ConfigDict(extra="forbid")

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
    # extra="forbid" (#143): request body for POST /api/demo-fixtures/{id}/load.
    model_config = ConfigDict(extra="forbid")

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

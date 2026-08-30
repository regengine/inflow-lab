from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from ..scenarios import ScenarioId
from .domain import DestinationMode, OperationScale


MockFrictionCode = Literal["invalid_key", "subscription_inactive", "rate_limit"]


class DeliveryConfig(BaseModel):
    # Unknown keys are rejected rather than dropped: a typo in a delivery
    # override used to return 200 having silently changed nothing.
    model_config = ConfigDict(extra="forbid")

    mode: DestinationMode = DestinationMode.MOCK
    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None
    # Failure-injection codes honored by mock delivery only, so operators can
    # rehearse the auth/billing/rate-limit friction a live integration hits.
    mock_friction: list[MockFrictionCode] = Field(default_factory=list)


class SimulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "codex-simulator"
    scenario: ScenarioId = ScenarioId.LEAFY_GREENS_SUPPLIER
    scale: OperationScale = OperationScale.MIDSIZE
    interval_seconds: float = 1.5
    batch_size: int = 3
    seed: int | None = 204
    persist_path: str = "data/events.jsonl"
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, value: float) -> float:
        if value < 0:
            raise ValueError("interval_seconds must be >= 0")
        return value

    @field_validator("batch_size")
    @classmethod
    def validate_batch_size(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("batch_size must be between 1 and 100")
        return value


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: SimulationConfig


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: SimulationConfig | None = None


class ResetRequest(BaseModel):
    """Config override for ``POST /api/simulate/reset``.

    The canonical shape is ``/start``'s — ``{"config": {...}}`` — so the same
    body works against both endpoints. Historically this endpoint took a bare
    ``SimulationConfig`` at the top level instead, and that shape is still
    accepted: the two are unambiguous because ``SimulationConfig`` has no
    ``config`` field and both models reject unknown keys.

    Before this existed, posting ``/start``'s shape here parsed fine and was
    then discarded wholesale, so the reset silently fell back to defaults.
    """

    model_config = ConfigDict(extra="forbid")

    config: SimulationConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_config(cls, data: Any) -> Any:
        if isinstance(data, dict) and data and "config" not in data:
            return {"config": data}
        return data


class StatusResponse(BaseModel):
    running: bool
    config: SimulationConfig
    stats: dict[str, Any]


class StepResponse(BaseModel):
    generated: int
    posted: int
    accepted: int = 0
    rejected: int = 0
    failed: int
    lot_codes: list[str]
    delivery_status: Literal["generated", "posted", "failed"]
    delivery_mode: DestinationMode
    delivery_attempts: int
    response: dict[str, Any] | None = None
    error: str | None = None


class ResetResponse(BaseModel):
    status: str

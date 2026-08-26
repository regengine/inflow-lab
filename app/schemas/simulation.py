from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from ..scenarios import ScenarioId
from .domain import DestinationMode, OperationScale


MockFrictionCode = Literal["invalid_key", "subscription_inactive", "rate_limit"]

# Request bodies reject unknown members instead of dropping them. This is a
# simulator whose job is to surface integration-contract mistakes, so a
# misspelled or misnested field has to fail loudly: silently ignoring it
# produced 200 OK responses that quietly applied nothing.
STRICT_REQUEST = ConfigDict(extra="forbid")


class DeliveryConfig(BaseModel):
    model_config = STRICT_REQUEST

    mode: DestinationMode = DestinationMode.MOCK
    endpoint: HttpUrl | None = None
    api_key: str | None = None
    tenant_id: str | None = None
    # Failure-injection codes honored by mock delivery only, so operators can
    # rehearse the auth/billing/rate-limit friction a live integration hits.
    mock_friction: list[MockFrictionCode] = Field(default_factory=list)


class SimulationConfig(BaseModel):
    model_config = STRICT_REQUEST

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
    model_config = STRICT_REQUEST

    config: SimulationConfig


class StepRequest(BaseModel):
    model_config = STRICT_REQUEST

    config: SimulationConfig | None = None


class ResetRequest(BaseModel):
    """Reset body, accepting both the wrapped and the historical flat shape.

    ``/start`` has always wrapped its override as ``{"config": {...}}``
    while ``/reset`` read the same fields unwrapped at the top level, so
    posting one endpoint's body to the other parsed cleanly and applied
    nothing. Both shapes are accepted here — the wrapped one so the two
    endpoints agree, the flat one because the console and existing
    integrations send it — and anything that is neither is a 422 rather
    than a silent no-op.
    """

    model_config = STRICT_REQUEST

    config: SimulationConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_flat_config(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if not data:
            return {"config": None}
        # A body carrying "config" is the wrapped shape; anything alongside
        # it is a misnesting mistake and `extra="forbid"` will say so.
        if "config" in data:
            return data
        return {"config": data}


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

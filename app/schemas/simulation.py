from __future__ import annotations

import ipaddress
import os
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from ..scenarios import ScenarioId
from .domain import DestinationMode, OperationScale

MockFrictionCode = Literal["invalid_key", "subscription_inactive", "rate_limit"]

# Outbound delivery posts the configured RegEngine API key to whatever endpoint
# is set, and nothing validated that endpoint. A link-local or loopback target
# therefore reached internal services *and* received the credential:
#
#     DIALED POST http://169.254.169.254/latest/meta-data/
#       X-RegEngine-API-Key=<the tenant's real key>
#
# The endpoint is operator-supplied, but Basic Auth is opt-in (`BasicAuthConfig.
# enabled` is false when the env vars are unset) and the integration routes
# carry no auth dependency, so on a default deployment this is unauthenticated.
ALLOW_PRIVATE_ENDPOINTS_ENV = "REGENGINE_ALLOW_PRIVATE_ENDPOINTS"
_LOOPBACK_HOSTNAMES = {"localhost", "ip6-localhost", "ip6-loopback"}


def _allow_private_endpoints() -> bool:
    """Escape hatch for pointing the simulator at a RegEngine on localhost."""
    return os.getenv(ALLOW_PRIVATE_ENDPOINTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _guard_endpoint(value: HttpUrl) -> HttpUrl:
    reason = endpoint_rejection_reason(str(value))
    if reason is not None:
        raise ValueError(reason)
    return value


# Shared so every body that can set an endpoint is guarded. `DeliveryConfig`
# alone is not enough: the integration bodies are merged into it with
# `model_copy(update=...)`, which does not re-validate.
GuardedEndpoint = Annotated[HttpUrl, AfterValidator(_guard_endpoint)]


def endpoint_rejection_reason(endpoint: str) -> str | None:
    """Why this endpoint must not be dialed, or None if it is acceptable.

    Deliberately does NOT resolve the hostname. Resolution here would be
    blocking I/O inside a Pydantic validator, i.e. on the event loop -- the
    same defect #216 was filed for. The cost is that a *name* resolving to a
    private address still passes; pinning the resolved address at connect time
    is the follow-up, and is what #207 asks for. What this does close is the
    literal-address case, which is the one that reaches cloud metadata.
    """
    if _allow_private_endpoints():
        return None

    parsed = urlparse(str(endpoint))
    host = parsed.hostname
    if not host:
        return "endpoint must include a host"

    if parsed.scheme != "https":
        return (
            f"endpoint must use https (got {parsed.scheme!r}); the RegEngine API key is sent "
            f"as a header and would otherwise cross the network in cleartext. "
            f"Set {ALLOW_PRIVATE_ENDPOINTS_ENV}=1 for local development."
        )

    lowered = host.lower().rstrip(".")
    if lowered in _LOOPBACK_HOSTNAMES or lowered.endswith(".localhost"):
        return (
            f"endpoint host {host!r} is loopback. "
            f"Set {ALLOW_PRIVATE_ENDPOINTS_ENV}=1 for local development."
        )

    try:
        address = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        return None  # A name. See the docstring on what this does not cover.

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return (
            f"endpoint address {address} is in a private, loopback, link-local or reserved "
            f"range, so delivery would reach an internal host and hand it the API key. "
            f"Set {ALLOW_PRIVATE_ENDPOINTS_ENV}=1 for local development."
        )
    return None


class DeliveryConfig(BaseModel):
    # Unknown keys are rejected rather than dropped: a typo in a delivery
    # override used to return 200 having silently changed nothing.
    model_config = ConfigDict(extra="forbid")

    mode: DestinationMode = DestinationMode.MOCK
    endpoint: GuardedEndpoint | None = None
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

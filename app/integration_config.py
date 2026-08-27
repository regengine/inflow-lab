"""How a delivery configuration is validated, merged, sanitized and reported.

Split out of ``controller.py``. Every function here is a pure projection of
config objects: given a stored `DeliveryConfig`/`SimulationConfig` and a
caller-supplied one, produce the config to use, the config safe to hand out,
or the status to render. None of it reads controller state beyond
``self.config``, none of it touches `_lock`, `_lifecycle_lock` or
`_store_epoch`, and none of it is part of the snapshot/deliver/commit
discipline -- which is exactly why it can leave while those stay.

The rules encoded here are subtle enough to be worth one place to read them:
which credentials survive an omitted field (#148), which are scrubbed on the
way out, and which are downgraded before being written to a scenario save.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from .contract import INFLOW_CONTRACT_VERSION
from .regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT, WEBHOOK_HMAC_SECRET_ENV
from .schemas.domain import DestinationMode
from .schemas.integration import IntegrationConfigureRequest, IntegrationStatusResponse
from .schemas.simulation import DeliveryConfig, SimulationConfig

__all__ = [
    "apply_integration_updates",
    "build_integration_status",
    "merge_delivery_credentials",
    "preserve_delivery_credentials",
    "sanitize_public_config",
    "sanitize_saved_config",
    "validate_live_delivery",
]


def validate_live_delivery(delivery: DeliveryConfig) -> None:
    if delivery.mode == DestinationMode.LIVE and (not delivery.api_key or not delivery.tenant_id):
        raise ValueError("Live delivery requires both api_key and tenant_id")


def merge_delivery_credentials(
    stored: DeliveryConfig, delivery: DeliveryConfig | None
) -> DeliveryConfig:
    """One delivery config with the caller's fields over the stored secrets.

    Scoped to live delivery, which is the only mode that uses credentials.
    Pointing the simulator back at the sandbox is also how an operator stops
    using a live tenant, so a mock/off config is taken at face value and
    drops the stored secret rather than keeping a live credential alive in a
    config that no longer sends to it.
    """
    if delivery is None:
        return stored
    if delivery.mode != DestinationMode.LIVE:
        return delivery
    updates: dict[str, Any] = {}
    if delivery.api_key is None and stored.api_key:
        updates["api_key"] = stored.api_key
    if delivery.tenant_id is None and stored.tenant_id:
        updates["tenant_id"] = stored.tenant_id
    if not updates:
        return delivery
    return delivery.model_copy(update=updates, deep=True)


def preserve_delivery_credentials(
    stored: DeliveryConfig, config: SimulationConfig
) -> SimulationConfig:
    """Fill in credentials the caller did not send from the stored config.

    `status()` scrubs `api_key` (and, in live mode, `tenant_id`) before it
    leaves the process, so a console tab that loaded after the credentials
    were configured has never seen them and cannot echo them back. Treating
    their absence as "clear them" meant Start, Reset and Retry silently
    downgraded a live integration (#148). An omitted or null credential
    therefore keeps the stored one, exactly as `/api/integration/configure`
    already behaves; clearing one is done there, explicitly.
    """
    delivery = merge_delivery_credentials(stored, config.delivery)
    if delivery is config.delivery:
        return config
    return config.model_copy(update={"delivery": delivery}, deep=True)


def sanitize_public_config(config: SimulationConfig) -> SimulationConfig:
    delivery = config.delivery.model_copy(update={"api_key": None}, deep=True)
    if delivery.mode == DestinationMode.LIVE:
        delivery = delivery.model_copy(update={"tenant_id": None}, deep=True)
    return config.model_copy(update={"delivery": delivery}, deep=True)


def sanitize_saved_config(config: SimulationConfig) -> SimulationConfig:
    delivery = config.delivery.model_copy(update={"api_key": None}, deep=True)
    if delivery.mode == DestinationMode.LIVE:
        delivery = delivery.model_copy(
            update={
                "mode": DestinationMode.MOCK,
                "endpoint": None,
                "tenant_id": None,
                "api_key": None,
            },
            deep=True,
        )
    return config.model_copy(update={"delivery": delivery}, deep=True)


def apply_integration_updates(
    delivery: DeliveryConfig, request: IntegrationConfigureRequest
) -> DeliveryConfig:
    """The delivery config `request` asks for, over the one in place.

    Only fields the caller actually sent are applied; an empty string for a
    credential is an explicit clear, which is what distinguishes this path
    from `merge_delivery_credentials`.
    """
    updates: dict[str, Any] = {}
    if request.mode is not None:
        updates["mode"] = request.mode
    if request.endpoint is not None:
        updates["endpoint"] = request.endpoint
    if request.api_key is not None:
        updates["api_key"] = request.api_key or None
    if request.tenant_id is not None:
        updates["tenant_id"] = request.tenant_id or None
    if request.mock_friction is not None:
        updates["mock_friction"] = list(request.mock_friction)
    return delivery.model_copy(update=updates, deep=True)


def build_integration_status(delivery: DeliveryConfig) -> IntegrationStatusResponse:
    endpoint = str(delivery.endpoint) if delivery.endpoint else DEFAULT_LIVE_INGEST_ENDPOINT
    return IntegrationStatusResponse(
        mode=delivery.mode,
        endpoint=endpoint,
        endpoint_host=urlparse(endpoint).netloc,
        api_key_configured=bool(delivery.api_key),
        tenant_configured=bool(delivery.tenant_id),
        hmac_configured=bool(os.getenv(WEBHOOK_HMAC_SECRET_ENV, "").strip()),
        contract_version=INFLOW_CONTRACT_VERSION,
        mock_friction=list(delivery.mock_friction),
    )

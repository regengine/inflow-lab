from __future__ import annotations

import pytest

from scripts.customer_journey import build_config, generate_batch, parse_redis_url, resp_command
from app.engine import LegitFlowEngine
from app.mock_service import validate_event_like_regengine
from app.scenarios import ScenarioId


def test_parse_redis_url_defaults() -> None:
    assert parse_redis_url("redis://localhost:6379/0") == ("localhost", 6379, 0, None)
    assert parse_redis_url("redis://redis") == ("redis", 6379, 0, None)
    assert parse_redis_url("rediss://:secret@cache.example:6380/2") == ("cache.example", 6380, 2, "secret")


def test_parse_redis_url_rejects_non_redis_schemes() -> None:
    with pytest.raises(ValueError):
        parse_redis_url("http://localhost:6379")


def test_resp_command_encodes_hset() -> None:
    assert resp_command("HSET", "billing:tenant:t1", "status", "trialing") == (
        b"*4\r\n$4\r\nHSET\r\n$17\r\nbilling:tenant:t1\r\n$6\r\nstatus\r\n$8\r\ntrialing\r\n"
    )


def test_journey_batches_are_canonical_for_regengine() -> None:
    engine = LegitFlowEngine()
    engine.reset(204, scenario=ScenarioId.FRESH_CUT_PROCESSOR)
    payload = generate_batch(engine, 15)
    for event in payload.events:
        assert validate_event_like_regengine(event) == [], event.cte_type


def test_journey_config_targets_live_delivery() -> None:
    config = build_config(
        "http://localhost:8000/api/v1/webhooks/ingest",
        "rge_test_key",
        "11111111-1111-1111-1111-111111111111",
    )
    assert config.delivery.mode.value == "live"
    assert str(config.delivery.endpoint) == "http://localhost:8000/api/v1/webhooks/ingest"


def test_provision_returns_the_key_id_needed_for_teardown() -> None:
    import asyncio

    import httpx

    from scripts.customer_journey import provision_tenant_and_key

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/admin/tenants":
            return httpx.Response(200, json={"tenant_id": "tenant-123"})
        if request.url.path == "/v1/admin/keys":
            return httpx.Response(200, json={"api_key": "rge_secret", "key_id": "key-456"})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await provision_tenant_and_key(client, "http://stack.test", "admin")

    provisioned = asyncio.run(run())

    assert provisioned.tenant_id == "tenant-123"
    assert provisioned.api_key == "rge_secret"
    assert provisioned.key_id == "key-456"
    # The raw API key must not leak through the dataclass repr.
    assert "rge_secret" not in repr(provisioned)


def test_deprovision_deletes_the_key_then_the_tenant() -> None:
    import asyncio

    import httpx

    from scripts.customer_journey import (
        JourneyReport,
        ProvisionedTenant,
        deprovision_tenant_and_key,
    )

    deleted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.headers["X-Admin-Key"] == "admin"
        deleted.append(request.url.path)
        return httpx.Response(204)

    report = JourneyReport()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await deprovision_tenant_and_key(
                client,
                "http://stack.test",
                "admin",
                ProvisionedTenant("tenant-123", "rge_secret", "key-456"),
                report,
            )

    asyncio.run(run())

    assert deleted == ["/v1/admin/keys/key-456", "/v1/admin/tenants/tenant-123"]
    assert report.failed is False


def test_deprovision_reports_manual_cleanup_when_the_admin_api_refuses() -> None:
    import asyncio

    import httpx

    from scripts.customer_journey import (
        JourneyReport,
        ProvisionedTenant,
        deprovision_tenant_and_key,
    )

    report = JourneyReport()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(405))
        ) as client:
            await deprovision_tenant_and_key(
                client,
                "http://stack.test",
                "admin",
                ProvisionedTenant("tenant-123", "rge_secret", None),
                report,
            )

    asyncio.run(run())

    assert report.failed is True
    assert "tenant-123" in report.steps[-1][2]

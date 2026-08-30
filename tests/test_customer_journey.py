from __future__ import annotations

import pytest

from app.engine import LegitFlowEngine
from app.mock_service import validate_event_like_regengine
from app.scenarios import ScenarioId
from scripts.customer_journey import build_config, generate_batch, parse_redis_url, resp_command


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

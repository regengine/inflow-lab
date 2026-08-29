from __future__ import annotations

import argparse
import asyncio
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest

from scripts import customer_journey
from scripts.customer_journey import (
    JourneyReport,
    build_config,
    generate_batch,
    parse_redis_url,
    record_billing_seed,
    resp_command,
    seed_billing_status,
)
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


# ---------------------------------------------------------------------------
# #109 -- the billing seed must detect a failed Redis write, not just an
# error reply. seed_billing_status used to treat any reply not starting with
# "-" as success, so a closed peer (empty read) or TLS alert bytes produced a
# PASS line for a write that never happened.
# ---------------------------------------------------------------------------


class _ScriptedRedis:
    """A single-connection TCP server that plays a fixed reply script."""

    def __init__(self, replies: list[bytes] | None, *, close_immediately: bool = False) -> None:
        self.replies = replies or []
        self.close_immediately = close_immediately
        self.received = b""
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def url(self, scheme: str = "redis", password: str | None = None) -> str:
        credential = f":{password}@" if password else ""
        return f"{scheme}://{credential}127.0.0.1:{self.port}/0"

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            try:
                if self.close_immediately:
                    # Give whatever the client sends first a chance to land so
                    # the test can assert on it, then drop the connection.
                    self.received += conn.recv(65536)
                    return
                for reply in self.replies:
                    data = conn.recv(65536)
                    if not data:
                        return
                    self.received += data
                    conn.sendall(reply)
            except OSError:
                return

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=3)


@pytest.fixture
def scripted_redis():
    servers: list[_ScriptedRedis] = []

    def factory(replies: list[bytes] | None = None, *, close_immediately: bool = False):
        server = _ScriptedRedis(replies, close_immediately=close_immediately)
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.close()


def test_seed_billing_status_accepts_a_well_formed_exchange(scripted_redis) -> None:
    server = scripted_redis([b":1\r\n", b"$8\r\ntrialing\r\n"])

    seed_billing_status(server.url(), "tenant-1", "trialing")

    assert b"HSET" in server.received


def test_seed_billing_status_raises_when_the_peer_closes(scripted_redis) -> None:
    server = scripted_redis(None, close_immediately=True)

    with pytest.raises(RuntimeError, match="closed the connection"):
        seed_billing_status(server.url(), "tenant-1", "trialing")


def test_seed_billing_status_raises_on_bytes_that_are_not_resp(scripted_redis) -> None:
    # What a TLS server answering a plaintext client actually sends back.
    server = scripted_redis([b"\x15\x03\x03\x00\x02\x02\x14\r\n"])

    with pytest.raises(RuntimeError, match="unrecognized RESP type"):
        seed_billing_status(server.url(), "tenant-1", "trialing")


def test_seed_billing_status_raises_when_hset_does_not_answer_an_integer(scripted_redis) -> None:
    server = scripted_redis([b"+QUEUED\r\n"])

    with pytest.raises(RuntimeError, match="HSET: expected a RESP integer"):
        seed_billing_status(server.url(), "tenant-1", "trialing")


def test_seed_billing_status_raises_when_the_readback_disagrees(scripted_redis) -> None:
    server = scripted_redis([b":1\r\n", b"$6\r\nactive\r\n"])

    with pytest.raises(RuntimeError, match="expected 'trialing'"):
        seed_billing_status(server.url(), "tenant-1", "trialing")


def test_billing_seed_records_fail_when_the_peer_closes(scripted_redis) -> None:
    server = scripted_redis(None, close_immediately=True)
    report = JourneyReport()

    record_billing_seed(report, server.url(), "tenant-1")

    assert report.failed is True
    name, ok, detail = report.steps[0]
    assert name == "Activate billing (Redis seed)"
    assert ok is False
    assert "402/503" in detail


def test_billing_seed_records_pass_on_a_confirmed_write(scripted_redis) -> None:
    server = scripted_redis([b":1\r\n", b"$8\r\ntrialing\r\n"])
    report = JourneyReport()

    record_billing_seed(report, server.url(), "tenant-1")

    assert report.failed is False


def test_rediss_url_never_writes_the_password_in_plaintext(scripted_redis) -> None:
    server = scripted_redis(None, close_immediately=True)

    with pytest.raises(OSError):
        seed_billing_status(server.url("rediss", password="s3cr3t-redis-password"), "tenant-1")

    # Whatever reached the server is a TLS ClientHello, not an AUTH command.
    assert b"s3cr3t-redis-password" not in server.received
    assert b"AUTH" not in server.received


# ---------------------------------------------------------------------------
# #190 -- --local provisions a tenant and an API key on every run and used to
# leave both behind.
# ---------------------------------------------------------------------------


class _StubLiveClient:
    def __init__(self, *, raise_on_connect: bool = False) -> None:
        self.raise_on_connect = raise_on_connect

    async def check_connection(self, config):
        if self.raise_on_connect:
            raise RuntimeError("connection probe exploded")
        return SimpleNamespace(verdict="connected", detail="ok")

    async def ingest(self, payload, config, idempotency_key=None):
        return SimpleNamespace(
            response={"accepted": len(payload.events), "rejected": 0, "events": []}
        )


def _admin_handler(calls: list[tuple[str, str]], *, delete_status: int = 204):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "DELETE":
            return httpx.Response(delete_status)
        if path == "/v1/admin/tenants":
            return httpx.Response(200, json={"tenant_id": "tenant-abc"})
        if path == "/v1/admin/keys":
            return httpx.Response(200, json={"api_key": "rge_journey_key", "key_id": "key-xyz"})
        return httpx.Response(200, json={})

    return handler


def _local_args() -> argparse.Namespace:
    return argparse.Namespace(
        local=True,
        confirm_live=False,
        batches=1,
        batch_size=1,
        seed=204,
        scale="small",
        friction=False,
    )


def _drive_local_journey(monkeypatch, calls, *, live_client, delete_status: int = 204):
    monkeypatch.setenv("REGENGINE_ADMIN_KEY", "admin-master-key")
    monkeypatch.setenv("REGENGINE_BASE_URL", "http://regengine.test")
    monkeypatch.setattr(customer_journey, "LiveRegEngineClient", lambda: live_client)
    monkeypatch.setattr(customer_journey, "seed_billing_status", lambda *a, **k: None)

    async def drive() -> int:
        transport = httpx.MockTransport(_admin_handler(calls, delete_status=delete_status))
        async with httpx.AsyncClient(transport=transport) as client:
            return await customer_journey.run_journey(_local_args(), client=client)

    return asyncio.run(drive())


def test_local_journey_deletes_what_it_provisioned(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    _drive_local_journey(monkeypatch, calls, live_client=_StubLiveClient())

    assert ("DELETE", "/v1/admin/keys/key-xyz") in calls
    assert ("DELETE", "/v1/admin/tenants/tenant-abc") in calls


def test_local_journey_deletes_what_it_provisioned_when_a_later_step_raises(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    with pytest.raises(RuntimeError, match="connection probe exploded"):
        _drive_local_journey(
            monkeypatch, calls, live_client=_StubLiveClient(raise_on_connect=True)
        )

    # The tenant exists from the moment provisioning returned, so its removal
    # cannot depend on the rest of the journey succeeding.
    assert ("DELETE", "/v1/admin/tenants/tenant-abc") in calls


def test_teardown_reports_a_failure_when_the_admin_api_has_no_delete_route() -> None:
    calls: list[tuple[str, str]] = []
    report = JourneyReport()

    async def drive() -> None:
        transport = httpx.MockTransport(_admin_handler(calls, delete_status=405))
        async with httpx.AsyncClient(transport=transport) as client:
            await customer_journey.deprovision_tenant_and_key(
                client, "http://regengine.test", "admin-master-key", "tenant-abc", "key-xyz", report
            )

    asyncio.run(drive())

    assert report.failed is True
    name, ok, detail = report.steps[-1]
    assert name == "Teardown: journey tenant + key removed"
    assert ok is False
    assert "tenant-abc" in detail

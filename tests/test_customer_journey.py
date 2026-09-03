from __future__ import annotations

import socket
import threading

import pytest

from scripts.customer_journey import (
    JourneyReport,
    RedisReplyError,
    build_config,
    generate_batch,
    parse_redis_url,
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


# ---------------------------------------------------------------------------
# #109 -- the billing seed used to call anything not starting with "-" a PASS
# ---------------------------------------------------------------------------


class FakeRedis:
    """A one-connection RESP server that replies from a scripted list.

    ``replies`` entries are raw bytes to send; the sentinel ``CLOSE`` hangs up
    instead, which is what a TLS-only server does to a plaintext client.
    """

    CLOSE = object()

    def __init__(self, replies):
        self.replies = list(replies)
        self.received = bytearray()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            for reply in self.replies:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                self.received.extend(chunk)
                if reply is self.CLOSE:
                    return
                try:
                    conn.sendall(reply)
                except OSError:
                    return

    def url(self, *, scheme: str = "redis", password: str | None = None) -> str:
        auth = f":{password}@" if password else ""
        return f"{scheme}://{auth}127.0.0.1:{self.port}/0"

    def close(self) -> None:
        self._sock.close()
        self._thread.join(timeout=2)


@pytest.fixture
def fake_redis():
    servers = []

    def build(replies):
        server = FakeRedis(replies)
        servers.append(server)
        return server

    yield build
    for server in servers:
        server.close()


def test_billing_seed_succeeds_on_a_well_formed_exchange(fake_redis) -> None:
    server = fake_redis([b":1\r\n", b"$8\r\ntrialing\r\n"])

    seed_billing_status(server.url(), "tenant-1", "trialing")

    assert b"HSET" in bytes(server.received)
    assert b"HGET" in bytes(server.received)


def test_billing_seed_raises_when_the_peer_closes_without_replying(fake_redis) -> None:
    """The false PASS from #109: an empty read used to return silently."""
    server = fake_redis([FakeRedis.CLOSE])

    with pytest.raises(RedisReplyError, match="closed the connection"):
        seed_billing_status(server.url(), "tenant-1")


def test_billing_seed_raises_on_non_resp_bytes(fake_redis) -> None:
    """TLS alert bytes do not start with '-', so they used to read as success."""
    server = fake_redis([b"\x15\x03\x01\x00\x02\x02\x28"])

    with pytest.raises(RedisReplyError, match="Truncated or non-RESP"):
        seed_billing_status(server.url(), "tenant-1")


def test_billing_seed_raises_when_hset_returns_the_wrong_resp_type(fake_redis) -> None:
    server = fake_redis([b"+OK\r\n"])

    with pytest.raises(RedisReplyError, match="expected integer"):
        seed_billing_status(server.url(), "tenant-1")


def test_billing_seed_raises_on_a_redis_error_reply(fake_redis) -> None:
    server = fake_redis([b"-NOAUTH Authentication required.\r\n"])

    with pytest.raises(RedisReplyError, match="NOAUTH"):
        seed_billing_status(server.url(), "tenant-1")


def test_billing_seed_raises_when_the_read_back_disagrees(fake_redis) -> None:
    """HSET can succeed against the wrong db or key prefix; prove the value."""
    server = fake_redis([b":1\r\n", b"$-1\r\n"])

    with pytest.raises(RedisReplyError, match="did not take effect"):
        seed_billing_status(server.url(), "tenant-1")


def test_rediss_url_never_writes_the_password_before_the_handshake(fake_redis) -> None:
    """#109's credential exposure: AUTH used to go out over a raw socket."""
    server = fake_redis([b"+OK\r\n", b":1\r\n", b"$8\r\ntrialing\r\n"])

    with pytest.raises(Exception) as excinfo:
        seed_billing_status(server.url(scheme="rediss", password="s3cret"), "tenant-1")

    assert not isinstance(excinfo.value, AssertionError)
    assert b"s3cret" not in bytes(server.received), (
        "the Redis password reached the wire in plaintext before the TLS "
        "handshake failed"
    )


def test_a_failed_seed_is_recorded_as_a_journey_failure(fake_redis, capsys) -> None:
    """The caller wraps this in try/except and records PASS/FAIL from it."""
    server = fake_redis([FakeRedis.CLOSE])
    report = JourneyReport()

    try:
        seed_billing_status(server.url(), "tenant-1")
        report.record("Activate billing (Redis seed)", True, "status=trialing")
    except Exception as exc:  # noqa: BLE001 - mirrors customer_journey.run
        report.record("Activate billing (Redis seed)", False, str(exc))

    assert report.failed
    assert "[FAIL] Activate billing (Redis seed)" in capsys.readouterr().out

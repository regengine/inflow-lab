"""Egress address pinning for live delivery (#207).

The egress guard used to validate a *name*: it resolved the delivery host,
checked the answers, and then handed the same name to httpx, which resolved it
again when it opened the socket. A hostile zone with a short TTL answers public
for the first lookup and loopback for the second, and the stored API key and
tenant id follow the second answer.

The fix resolves once and dials the address that resolution approved, carrying
the original hostname in the `Host` header and in TLS SNI. These tests assert
the three things that has to mean:

* the address the guard approved is the address the socket is opened to, and
  nothing resolves the name a second time;
* certificate verification still happens against the *hostname*, never against
  the pinned literal -- proven against a real TLS handshake, with negative
  controls, because getting this wrong is worse than the gap it closes;
* a rebinding resolver whose later answers are loopback produces no connection
  to loopback at all.

Plus the three operator escape hatches, which must keep behaving exactly as
they did: `REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS`,
`REGENGINE_ALLOWED_DELIVERY_HOSTS` and `REGENGINE_DELIVERY_DNS_GUARD`.
"""

from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from app.regengine_client import (
    ALLOW_PRIVATE_DELIVERY_ENV,
    ALLOWED_DELIVERY_HOSTS_ENV,
    BLOCKED_ENDPOINT_VERDICT,
    DELIVERY_DNS_GUARD_ENV,
    BlockedDeliveryEndpointError,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
    PinnedEndpoint,
    _dial_pin,
    _PinnedAddressTransport,
    assert_delivery_endpoint_allowed,
    resolve_delivery_endpoint,
)
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig


HOSTILE_HOST = "rebind.attacker.example"
HOSTILE_ENDPOINT = f"https://{HOSTILE_HOST}/api/v1/webhooks/ingest"
# `ipaddress` treats the documentation ranges as private, which is right for a
# delivery endpoint but useless as a stand-in for "a public answer". These
# tests therefore use a genuinely public literal for the answers the guard has
# to accept, and never let a connection to it happen.
PUBLIC_LITERAL = "93.184.216.34"


@pytest.fixture(autouse=True)
def _direct_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning is deliberately suppressed behind an environment proxy.

    A proxied client never resolves the name itself, so there is no second
    lookup to pin, and httpcore's tunnel ignores `sni_hostname`. Some CI and
    sandbox environments export a proxy; these tests are about the unproxied
    path, so they clear it.
    """
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def make_payload() -> IngestPayload:
    return IngestPayload(
        source="egress-pinning-tests",
        events=[
            RegEngineEvent(
                cte_type=CTEType.RECEIVING,
                traceability_lot_code="00012345678901-LOT-2026-001",
                product_description="Romaine Lettuce",
                quantity=10,
                unit_of_measure="cases",
                location_name="Distribution Center #4",
                timestamp=datetime(2026, 2, 5, 8, 30, tzinfo=UTC),
                kdes={
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                },
            )
        ],
    )


def make_live_config(endpoint: str) -> SimulationConfig:
    return SimulationConfig.model_validate(
        {
            "delivery": {
                "mode": "live",
                "endpoint": endpoint,
                "api_key": "rge_live_secret_key",
                "tenant_id": "11111111-1111-1111-1111-111111111111",
            }
        }
    )


class RecordingResolver:
    """A resolver that answers differently on successive lookups.

    Answer 1 is public, so the guard is satisfied. Every later answer is
    loopback -- the classic rebinding zone. Thread-safe because the event
    loop resolves in an executor thread.
    """

    def __init__(self, host: str, answers: list[list[str]]) -> None:
        self._host = host
        self._answers = answers
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self._real = socket.getaddrinfo

    def __call__(self, host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        if host != self._host:
            return self._real(host, port, *args, **kwargs)
        with self._lock:
            self.calls.append(host)
            index = min(len(self.calls) - 1, len(self._answers) - 1)
            answers = self._answers[index]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
            for address in answers
        ]


class DialRecorder:
    """Records every TCP connect attempt at the layer that opens the socket."""

    def __init__(self) -> None:
        self.dials: list[tuple[str, int]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from httpcore._backends.anyio import AnyIOBackend

        recorder = self

        async def fake_connect_tcp(
            self: Any,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> Any:
            recorder.dials.append((host, port))
            raise httpcore.ConnectError("intercepted before any bytes were sent")

        monkeypatch.setattr(AnyIOBackend, "connect_tcp", fake_connect_tcp)


# --- 1. one resolution, and the validated address is the dialed address -----


def test_guard_returns_the_address_it_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = RecordingResolver(HOSTILE_HOST, [[PUBLIC_LITERAL]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    pin = assert_delivery_endpoint_allowed(HOSTILE_ENDPOINT)

    assert pin == PinnedEndpoint(hostname=HOSTILE_HOST, address=PUBLIC_LITERAL)
    assert resolver.calls == [HOSTILE_HOST]


def test_live_ingest_resolves_once_and_dials_that_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = RecordingResolver(HOSTILE_HOST, [[PUBLIC_LITERAL]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    recorder = DialRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(LiveRegEngineDeliveryError):
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), make_live_config(HOSTILE_ENDPOINT)))

    # Exactly one lookup for the whole request: the guard's. httpx got a URL
    # whose host is already a literal, so it had nothing left to resolve.
    assert resolver.calls == [HOSTILE_HOST]
    assert recorder.dials == [(PUBLIC_LITERAL, 443)]


def test_connection_check_resolves_once_and_dials_that_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = RecordingResolver(HOSTILE_HOST, [[PUBLIC_LITERAL]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    recorder = DialRecorder()
    recorder.install(monkeypatch)

    result = asyncio.run(
        LiveRegEngineClient().check_connection(make_live_config(HOSTILE_ENDPOINT))
    )

    assert result.verdict == "unreachable"
    assert resolver.calls == [HOSTILE_HOST]
    assert {host for host, _ in recorder.dials} == {PUBLIC_LITERAL}


def test_guard_resolution_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async guard must resolve off-thread, not inline.

    A slow resolver otherwise stalls every other request in the process for the
    full DNS timeout, on every live delivery and every connection test.
    """
    started = threading.Event()
    release = threading.Event()

    def slow_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        started.set()
        assert release.wait(timeout=5), "resolver was never released"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_LITERAL, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)

    async def scenario() -> tuple[PinnedEndpoint | None, int]:
        ticks = 0
        task = asyncio.ensure_future(resolve_delivery_endpoint(HOSTILE_ENDPOINT))
        while not started.is_set():
            await asyncio.sleep(0)
        # The loop keeps running while the resolver is parked in its thread.
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        release.set()
        return await task, ticks

    pin, ticks = asyncio.run(scenario())

    assert pin == PinnedEndpoint(hostname=HOSTILE_HOST, address=PUBLIC_LITERAL)
    assert ticks == 5


# --- 2. TLS is verified against the hostname, not the pinned literal --------


def _write_test_certificate(directory: Path, common_name: str) -> tuple[Path, Path]:
    """A self-signed cert for `common_name`, usable as its own trust anchor."""
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/CN={common_name}",
            "-addext",
            f"subjectAltName=DNS:{common_name}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._record()
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.do_GET()

    def _record(self) -> None:
        type(self).received.append({"path": self.path, "host": self.headers.get("Host", "")})

    def log_message(self, *args: Any) -> None:  # pragma: no cover - silence stderr
        return


class _Server:
    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        _RecordingHandler.received = []
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        if ssl_context is not None:
            self._httpd.socket = ssl_context.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def received(self) -> list[dict[str, str]]:
        return _RecordingHandler.received

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def tls_origin(tmp_path: Path) -> Any:
    if shutil.which("openssl") is None:  # pragma: no cover - environment dependent
        pytest.skip("openssl is required to mint the test certificate")
    name = "pinned.regengine.test"
    cert, key = _write_test_certificate(tmp_path, name)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server = _Server(ssl_context=context)
    try:
        yield name, server, cert
    finally:
        server.close()


def _ca_context(cert: Path) -> ssl.SSLContext:
    """A verifying SSL context that trusts exactly `cert`."""
    return ssl.create_default_context(cafile=str(cert))


def _get_via_transport(
    transport: httpx.AsyncBaseTransport, url: str
) -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            return await client.get(url)

    return asyncio.run(run())


def test_pinned_dial_verifies_the_certificate_against_the_hostname(
    tls_origin: Any,
) -> None:
    """The whole point: dial the literal, verify the name.

    The server's certificate carries `DNS:pinned.regengine.test` and no IP
    SAN, and the client trusts only that certificate. A handshake that
    succeeded against `127.0.0.1` would be impossible; success therefore proves
    verification used the hostname carried in `sni_hostname`.
    """
    name, server, cert = tls_origin
    transport = _PinnedAddressTransport(
        PinnedEndpoint(hostname=name, address="127.0.0.1"),
        verify=_ca_context(cert),
    )

    response = _get_via_transport(transport, f"https://{name}:{server.port}/health")

    assert response.status_code == 200
    # The origin saw its own name, not the address that was dialed.
    assert server.received[0]["host"] == f"{name}:{server.port}"


def test_pinned_dial_without_sni_would_fail_verification(tls_origin: Any) -> None:
    """Negative control: the `sni_hostname` extension is load-bearing."""
    name, server, cert = tls_origin

    class _NoSniTransport(_PinnedAddressTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            request.url = request.url.copy_with(host="127.0.0.1")
            return await httpx.AsyncHTTPTransport.handle_async_request(self, request)

    transport = _NoSniTransport(
        PinnedEndpoint(hostname=name, address="127.0.0.1"), verify=_ca_context(cert)
    )

    with pytest.raises(httpx.ConnectError) as excinfo:
        _get_via_transport(transport, f"https://{name}:{server.port}/health")

    assert "certificate" in str(excinfo.value).lower()
    assert server.received == []


def test_pinned_dial_rejects_a_certificate_for_another_name(tls_origin: Any) -> None:
    """Negative control: verification follows the pinned hostname, not luck."""
    name, server, cert = tls_origin
    transport = _PinnedAddressTransport(
        PinnedEndpoint(hostname="other.regengine.test", address="127.0.0.1"),
        verify=_ca_context(cert),
    )

    with pytest.raises(httpx.ConnectError) as excinfo:
        _get_via_transport(transport, "https://other.regengine.test:%d/health" % server.port)

    assert "other.regengine.test" in str(excinfo.value)
    assert server.received == []


def test_pinned_dial_leaves_other_hosts_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the pinned host is rewritten; anything else goes out unchanged."""
    recorder = DialRecorder()
    recorder.install(monkeypatch)
    transport = _PinnedAddressTransport(
        PinnedEndpoint(hostname="somewhere.else.test", address=PUBLIC_LITERAL)
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            await client.get("https://elsewhere.regengine.test/health")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(run())

    assert recorder.dials == [("elsewhere.regengine.test", 443)]


# --- 3. a rebinding resolver never reaches loopback ------------------------


def test_rebinding_resolver_never_reaches_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First answer public, every later answer loopback: nothing lands local.

    A real loopback listener stands in for the attacker's target. It must see
    no request at all -- not a rejected one, not an empty one.
    """
    attacker = _Server()
    try:
        resolver = RecordingResolver(
            HOSTILE_HOST,
            [[PUBLIC_LITERAL], ["127.0.0.1"]],
        )
        monkeypatch.setattr(socket, "getaddrinfo", resolver)
        recorder = DialRecorder()
        recorder.install(monkeypatch)
        endpoint = f"https://{HOSTILE_HOST}:{attacker.port}/api/v1/webhooks/ingest"

        with pytest.raises(LiveRegEngineDeliveryError):
            asyncio.run(
                LiveRegEngineClient().ingest(make_payload(), make_live_config(endpoint))
            )

        assert resolver.calls == [HOSTILE_HOST]
        assert recorder.dials == [(PUBLIC_LITERAL, attacker.port)]
        assert all(host != "127.0.0.1" for host, _ in recorder.dials)
        assert attacker.received == []
    finally:
        attacker.close()


def test_rebinding_resolver_never_reaches_loopback_without_interception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same scenario with a real socket layer, no connect stub.

    The pinned address is a plain HTTP loopback listener the *guard* would have
    refused had it been answered first; here the first answer is a port nothing
    is listening on, so the delivery fails as a connection error. What matters
    is that the attacker's listener is never touched even though every lookup
    after the first says loopback.
    """
    attacker = _Server()
    dead_port = _free_port()
    try:
        resolver = RecordingResolver(
            HOSTILE_HOST,
            [[PUBLIC_LITERAL], ["127.0.0.1"], ["127.0.0.1"]],
        )
        monkeypatch.setattr(socket, "getaddrinfo", resolver)
        monkeypatch.setenv("REGENGINE_LIVE_TIMEOUT_SECONDS", "1")
        # The attacker's listener is plain HTTP (no TLS to terminate), so this
        # endpoint has to be `http`. That is the scheme policy's business, not
        # this test's: opt in so the pinning behaviour under test is what the
        # guard actually reaches.
        monkeypatch.setenv("REGENGINE_ALLOW_CLEARTEXT_DELIVERY", "1")
        # Dial a public literal on a port nothing answers: the request fails,
        # and crucially it fails *there* rather than succeeding on loopback.
        endpoint = f"http://{HOSTILE_HOST}:{dead_port}/api/v1/webhooks/ingest"

        with pytest.raises(LiveRegEngineDeliveryError):
            asyncio.run(
                LiveRegEngineClient().ingest(make_payload(), make_live_config(endpoint))
            )

        assert attacker.received == []
        assert resolver.calls == [HOSTILE_HOST]
    finally:
        attacker.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_first_answer_loopback_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning did not weaken the original check."""
    resolver = RecordingResolver(HOSTILE_HOST, [["127.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    with pytest.raises(BlockedDeliveryEndpointError):
        assert_delivery_endpoint_allowed(HOSTILE_ENDPOINT)


def test_any_private_answer_is_refused_not_merely_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public first answer does not license a private second one."""
    resolver = RecordingResolver(HOSTILE_HOST, [[PUBLIC_LITERAL, "10.0.0.5"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    with pytest.raises(BlockedDeliveryEndpointError):
        assert_delivery_endpoint_allowed(HOSTILE_ENDPOINT)


def test_blocked_endpoint_check_still_refuses_before_any_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = DialRecorder()
    recorder.install(monkeypatch)

    result = asyncio.run(
        LiveRegEngineClient().check_connection(
            make_live_config("http://169.254.169.254/latest/meta-data/")
        )
    )

    assert result.verdict == BLOCKED_ENDPOINT_VERDICT
    assert recorder.dials == []


# --- 4. the operator escape hatches still behave exactly as before ---------


def test_private_host_opt_out_allows_localhost_and_does_not_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_PRIVATE_DELIVERY_ENV, "1")
    resolver = RecordingResolver("localhost", [["127.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    pin = assert_delivery_endpoint_allowed("http://localhost:8000/api/v1/webhooks/ingest")

    assert pin is None
    # The opt-out path does not resolve at all, so it cannot be slowed or
    # broken by a resolver that a local-only developer may not even have.
    assert resolver.calls == []


def test_private_host_opt_out_reaches_a_real_local_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end against a loopback listener, the local-development case."""
    local = _Server()
    try:
        monkeypatch.setenv(ALLOW_PRIVATE_DELIVERY_ENV, "1")
        endpoint = f"http://localhost:{local.port}/api/v1/webhooks/ingest"

        result = asyncio.run(
            LiveRegEngineClient().ingest(make_payload(), make_live_config(endpoint))
        )

        assert result.response == {"ok": True}
        assert local.received[0]["path"] == "/api/v1/webhooks/ingest"
        assert local.received[0]["host"] == f"localhost:{local.port}"
    finally:
        local.close()


def test_builtin_mock_endpoint_still_reachable_under_the_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built-in stand-in is served from this process over loopback."""
    mock = _Server()
    try:
        monkeypatch.setenv(ALLOW_PRIVATE_DELIVERY_ENV, "1")
        endpoint = f"http://127.0.0.1:{mock.port}/api/v1/webhooks/ingest"

        result = asyncio.run(
            LiveRegEngineClient().check_connection(make_live_config(endpoint))
        )

        # A 200 from the probe path; the verdict itself depends on the stand-in's
        # health document, so the assertion is that it was reached at all.
        assert result.verdict != BLOCKED_ENDPOINT_VERDICT
        assert result.verdict != "unreachable"
        assert any(entry["path"].startswith("/api/v1/webhooks/recent") for entry in mock.received)
    finally:
        mock.close()


def test_host_allowlist_still_gates_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWED_DELIVERY_HOSTS_ENV, ".regengine.co")
    resolver = RecordingResolver(HOSTILE_HOST, [[PUBLIC_LITERAL]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    with pytest.raises(BlockedDeliveryEndpointError):
        assert_delivery_endpoint_allowed(HOSTILE_ENDPOINT)
    assert resolver.calls == []


def test_host_allowlist_still_admits_the_listed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWED_DELIVERY_HOSTS_ENV, ".regengine.co")
    resolver = RecordingResolver("www.regengine.co", [[PUBLIC_LITERAL]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    pin = assert_delivery_endpoint_allowed("https://www.regengine.co/api/v1/webhooks/ingest")

    assert pin == PinnedEndpoint(hostname="www.regengine.co", address=PUBLIC_LITERAL)


def test_dns_guard_off_skips_resolution_and_pinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sealed environments switch the DNS step off; that must stay switchable."""
    monkeypatch.setenv(DELIVERY_DNS_GUARD_ENV, "0")
    resolver = RecordingResolver(HOSTILE_HOST, [["127.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    assert assert_delivery_endpoint_allowed(HOSTILE_ENDPOINT) is None
    assert resolver.calls == []


def test_ip_literal_endpoint_is_not_re_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned literal must not trip the guard on a later pass.

    A pinned dial rewrites the URL host to an address. Re-validating that URL
    has to stay possible without the address being mistaken for something the
    guard should re-resolve or refuse.
    """
    resolver = RecordingResolver(PUBLIC_LITERAL, [["127.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    assert assert_delivery_endpoint_allowed(f"https://{PUBLIC_LITERAL}/ingest") is None
    assert resolver.calls == []


def test_proxy_environment_suppresses_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behind a proxy the client never resolves, so nothing is pinned.

    httpcore takes the TLS `server_hostname` from the request origin on a
    tunnelled connection and ignores `sni_hostname`, and handing httpx an
    explicit transport disables its environment proxy mounts. Pinning there
    would break delivery and route around the operator's proxy, so it is
    suppressed -- deliberately, and only there.
    """
    resolver = RecordingResolver(HOSTILE_HOST, [[PUBLIC_LITERAL]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")

    assert asyncio.run(_dial_pin(HOSTILE_ENDPOINT)) is None
    # Still validated: a proxy is not a way past the guard.
    monkeypatch.setattr(socket, "getaddrinfo", RecordingResolver(HOSTILE_HOST, [["127.0.0.1"]]))
    with pytest.raises(BlockedDeliveryEndpointError):
        asyncio.run(_dial_pin(HOSTILE_ENDPOINT))


# --- 5. a real public HTTPS endpoint ---------------------------------------


LIVE_CHECK_ENV = "REGENGINE_EGRESS_LIVE_CHECK"
LIVE_CHECK_HOST_ENV = "REGENGINE_EGRESS_LIVE_HOST"
LIVE_CHECK_CA_ENV = "REGENGINE_EGRESS_LIVE_CA"


@pytest.mark.skipif(
    os.getenv(LIVE_CHECK_ENV, "").strip().lower() not in {"1", "true", "yes", "on"},
    reason=f"set {LIVE_CHECK_ENV}=1 to dial a real public HTTPS host",
)
def test_pinned_dial_against_a_real_https_endpoint() -> None:
    """Opt-in: prove the pinned dial completes a real public TLS handshake.

    Off by default because the suite must stay hermetic. Point
    `REGENGINE_EGRESS_LIVE_HOST` at a host to use (default www.regengine.co)
    and `REGENGINE_EGRESS_LIVE_CA` at a CA bundle if the network intercepts
    TLS.
    """
    host = os.getenv(LIVE_CHECK_HOST_ENV, "").strip() or "www.regengine.co"
    ca = os.getenv(LIVE_CHECK_CA_ENV, "").strip()
    endpoint = f"https://{host}/"

    pin = assert_delivery_endpoint_allowed(endpoint)
    assert pin is not None, f"{host} did not resolve"

    transport = _PinnedAddressTransport(
        pin, **({"verify": _ca_context(Path(ca))} if ca else {})
    )
    response = _get_via_transport(transport, endpoint)

    assert response.status_code < 500

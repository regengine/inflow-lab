from __future__ import annotations

import json

import httpx

from scripts.live_trial import main

BASE_ENV = {
    "REGENGINE_REMOTE_BASE_URL": "https://demo.example.com",
    "REGENGINE_REMOTE_USERNAME": "demo",
    "REGENGINE_REMOTE_PASSWORD": "demo-password",
    "REGENGINE_REMOTE_TENANT": "live-trial-smoke",
}
LIVE_ENV = {
    **BASE_ENV,
    "REGENGINE_LIVE_ENDPOINT": "https://www.regengine.co/api/v1/webhooks/ingest",
    "REGENGINE_LIVE_API_KEY": "live-api-secret",
    "REGENGINE_LIVE_TENANT_ID": "live-tenant-secret",
}


def test_live_trial_refuses_without_explicit_mode(capsys):
    server = FakeLiveTrialServer()
    with httpx.Client(
        base_url=BASE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main([], environ=LIVE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--confirm-live" in captured.err
    assert server.requests == []


def test_live_trial_dry_run_only_uses_mock_delivery_without_live_credentials(capsys):
    server = FakeLiveTrialServer()
    with httpx.Client(
        base_url=BASE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(["--dry-run-only"], environ=BASE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Live trial dry-run passed" in captured.out
    assert "demo-password" not in captured.out
    assert server.live_step_count == 0
    assert [body["delivery"]["mode"] for body in server.reset_bodies] == ["mock"]
    assert all("authorization" in request.headers for request in server.requests)
    assert all(
        request.headers["x-regengine-tenant"] == "live-trial-smoke"
        for request in server.requests
    )


def test_live_trial_confirm_live_runs_mock_then_exactly_one_live_batch(capsys):
    server = FakeLiveTrialServer()
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(["--confirm-live"], environ=LIVE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Live trial completed" in captured.out
    assert "live_posted=1" in captured.out
    assert "live_failed=0" in captured.out
    assert "demo-password" not in captured.out
    assert "live-api-secret" not in captured.out
    assert "live-tenant-secret" not in captured.out
    assert server.live_step_count == 1
    assert "Delivery reverted to mock for tenant live-trial-smoke" in captured.out
    # The trailing "mock" is the disarm: without it the tenant controller keeps
    # the customer's live endpoint and API key for the lifetime of the process.
    assert [body["delivery"]["mode"] for body in server.reset_bodies] == [
        "mock",
        "live",
        "mock",
    ]
    assert server.reset_bodies[2]["delivery"] == {"mode": "mock"}
    assert server.integration_status_reads == 1
    assert server.reset_bodies[1]["delivery"] == {
        "mode": "live",
        "endpoint": LIVE_ENV["REGENGINE_LIVE_ENDPOINT"],
        "api_key": LIVE_ENV["REGENGINE_LIVE_API_KEY"],
        "tenant_id": LIVE_ENV["REGENGINE_LIVE_TENANT_ID"],
    }
    assert server.step_batch_sizes == ["1", "1"]


def test_live_trial_reverts_delivery_to_mock_when_the_live_batch_fails(capsys):
    """The disarm has to run on the failure path too -- that is the dangerous one.

    A live batch that raises leaves the operator with a failed trial and, before
    this, a demo tenant still holding the customer's production credentials.
    """
    server = FakeLiveTrialServer(live_step_status=502)
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(["--confirm-live"], environ=LIVE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert [body["delivery"]["mode"] for body in server.reset_bodies] == [
        "mock",
        "live",
        "mock",
    ]
    assert server.final_mode == "mock"
    assert "Delivery reverted to mock for tenant live-trial-smoke" in captured.out


def test_live_trial_fails_when_the_tenant_stays_armed_after_the_revert(capsys):
    """A revert that does not take effect must fail the run, not pass quietly."""
    server = FakeLiveTrialServer(ignore_disarm=True)
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(["--confirm-live"], environ=LIVE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "did not revert to mock" in captured.err
    assert "live-api-secret" not in captured.err


def test_live_trial_dry_run_never_touches_live_delivery():
    """No arm means no disarm: the dry run leaves a single mock reset behind."""
    server = FakeLiveTrialServer()
    with httpx.Client(
        base_url=BASE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        assert main(["--dry-run-only"], environ=BASE_ENV, client=client) == 0

    assert [body["delivery"]["mode"] for body in server.reset_bodies] == ["mock"]
    assert server.integration_status_reads == 0


def test_live_trial_confirm_live_requires_live_environment(capsys):
    server = FakeLiveTrialServer()
    with httpx.Client(
        base_url=BASE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(["--confirm-live"], environ=BASE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "REGENGINE_LIVE_ENDPOINT" in captured.err
    assert server.requests == []


class FakeLiveTrialServer:
    """Stands in for the demo instance, tracking delivery mode across resets.

    ``ignore_disarm`` models a server that accepts the revert POST but keeps the
    live config anyway; ``live_step_status`` makes the live batch fail so the
    failure-path revert can be observed.
    """

    def __init__(
        self, *, live_step_status: int = 200, ignore_disarm: bool = False
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.reset_bodies: list[dict] = []
        self.current_mode = "mock"
        self.live_api_key: str | None = None
        self.live_tenant_id: str | None = None
        self.live_step_count = 0
        self.step_batch_sizes: list[str | None] = []
        self.integration_status_reads = 0
        self.live_step_status = live_step_status
        self.ignore_disarm = ignore_disarm

    @property
    def final_mode(self) -> str:
        return self.current_mode

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/simulate/stop":
            return httpx.Response(200, json={"running": False})
        if path == "/api/integration/status":
            self.integration_status_reads += 1
            return httpx.Response(
                200,
                json={
                    "mode": self.current_mode,
                    "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
                    "endpoint_host": "www.regengine.co",
                    "api_key_configured": bool(self.live_api_key),
                    "tenant_configured": bool(self.live_tenant_id),
                    "hmac_configured": False,
                    "contract_version": "test",
                    "mock_friction": [],
                },
            )
        if path == "/api/simulate/reset":
            body = decode_json(request)
            self.reset_bodies.append(body)
            delivery = body["delivery"]
            if delivery["mode"] == "mock" and self.ignore_disarm:
                return httpx.Response(200, json={"status": "reset"})
            self.current_mode = delivery["mode"]
            # A real reset replaces the whole config, so a mock body drops the
            # stored credentials along with the mode.
            self.live_api_key = delivery.get("api_key")
            self.live_tenant_id = delivery.get("tenant_id")
            return httpx.Response(200, json={"status": "reset"})
        if path == "/api/simulate/step":
            batch_size = request.url.params.get("batch_size")
            self.step_batch_sizes.append(batch_size)
            if self.current_mode == "live":
                self.live_step_count += 1
                if self.live_step_status != 200:
                    return httpx.Response(
                        self.live_step_status, text="upstream ingest unavailable"
                    )
            return httpx.Response(
                200,
                json={
                    "generated": 1,
                    "posted": 1,
                    "failed": 0,
                    "lot_codes": ["TLC-LIVE-TRIAL-001"],
                    "delivery_status": "posted",
                    "delivery_mode": self.current_mode,
                    "delivery_attempts": 1,
                    "response": {},
                    "error": None,
                },
            )
        return httpx.Response(404, text=f"Unhandled path {path}")


def decode_json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


def test_live_trial_closes_the_client_it_creates(monkeypatch):
    """run_live_trial owns any client it constructs, so it must close it."""
    server = FakeLiveTrialServer()
    created: list[httpx.Client] = []
    real_client_cls = httpx.Client

    def fake_client(**kwargs):
        kwargs.pop("verify", None)
        client = real_client_cls(**kwargs, transport=httpx.MockTransport(server.handle))
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", fake_client)

    exit_code = main(["--dry-run-only"], environ=BASE_ENV)

    assert exit_code == 0
    assert len(created) == 1
    assert created[0].is_closed


def test_live_trial_leaves_a_caller_supplied_client_open():
    server = FakeLiveTrialServer()
    client = httpx.Client(
        base_url=BASE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    )
    try:
        exit_code = main(["--dry-run-only"], environ=BASE_ENV, client=client)
        assert exit_code == 0
        assert not client.is_closed
    finally:
        client.close()


def test_live_trial_rejects_a_base_url_outside_the_host_allowlist(capsys):
    """The demo base URL carries the shared Basic Auth secrets on every request.

    Same guard remote_smoke.py uses, from the same shared helper.
    """
    environ = {**BASE_ENV, "REGENGINE_REMOTE_BASE_URL": "https://attacker.example"}

    exit_code = main(["--dry-run-only"], environ=environ)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not allowed" in captured.err


def test_live_trial_allows_any_live_ingest_endpoint():
    """The live endpoint gets its own API key, never the demo Basic Auth."""
    from scripts.live_trial import config_from_env

    config = config_from_env(LIVE_ENV, require_live=True)

    assert config.live_endpoint == LIVE_ENV["REGENGINE_LIVE_ENDPOINT"]

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
    assert [body["delivery"]["mode"] for body in server.reset_bodies] == ["mock", "live"]
    assert server.reset_bodies[1]["delivery"] == {
        "mode": "live",
        "endpoint": LIVE_ENV["REGENGINE_LIVE_ENDPOINT"],
        "api_key": LIVE_ENV["REGENGINE_LIVE_API_KEY"],
        "tenant_id": LIVE_ENV["REGENGINE_LIVE_TENANT_ID"],
    }
    assert server.step_batch_sizes == ["1", "1"]


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
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reset_bodies: list[dict] = []
        self.current_mode = "mock"
        self.live_step_count = 0
        self.step_batch_sizes: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/simulate/stop":
            return httpx.Response(200, json={"running": False})
        if path == "/api/simulate/reset":
            body = decode_json(request)
            self.reset_bodies.append(body)
            self.current_mode = body["delivery"]["mode"]
            return httpx.Response(200, json={"status": "reset"})
        if path == "/api/simulate/step":
            batch_size = request.url.params.get("batch_size")
            self.step_batch_sizes.append(batch_size)
            if self.current_mode == "live":
                self.live_step_count += 1
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

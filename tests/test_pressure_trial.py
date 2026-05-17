from __future__ import annotations

import json
from threading import Lock

import httpx

from scripts.pressure_trial import main, parse_args, parse_stages, pressure_config_from_env_and_args


BASE_ENV = {
    "REGENGINE_REMOTE_BASE_URL": "https://demo.example.com",
    "REGENGINE_REMOTE_USERNAME": "demo",
    "REGENGINE_REMOTE_PASSWORD": "demo-password",
    "REGENGINE_REMOTE_TENANT": "pressure-trial-smoke",
}
LIVE_ENV = {
    **BASE_ENV,
    "REGENGINE_LIVE_ENDPOINT": "https://www.regengine.co/api/v1/webhooks/ingest",
    "REGENGINE_LIVE_API_KEY": "live-api-secret",
    "REGENGINE_LIVE_TENANT_ID": "live-tenant-secret",
}


def test_parse_stages_parses_requests_and_batch_sizes():
    stages = parse_stages("2x1,3x25")
    assert [(stage.requests, stage.batch_size) for stage in stages] == [(2, 1), (3, 25)]


def test_pressure_trial_dry_run_only_uses_mock_delivery_without_live_credentials(capsys):
    server = FakePressureTrialServer()
    with httpx.Client(
        base_url=BASE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(["--dry-run-only"], environ=BASE_ENV, client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Pressure trial dry-run passed" in captured.out
    assert server.live_step_count == 0
    assert [body["delivery"]["mode"] for body in server.reset_bodies] == ["mock"]


def test_pressure_trial_confirm_live_runs_mock_then_staged_live_batches(capsys):
    server = FakePressureTrialServer()
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(
            ["--confirm-live", "--stages", "2x1,3x4"],
            environ=LIVE_ENV,
            client=client,
        )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Pressure trial completed" in captured.out
    assert "total_requests=5" in captured.out
    assert "total_events_requested=14" in captured.out
    assert "total_posted=14" in captured.out
    assert "demo-password" not in captured.out
    assert "live-api-secret" not in captured.out
    assert "live-tenant-secret" not in captured.out
    assert server.live_step_count == 5
    assert [body["delivery"]["mode"] for body in server.reset_bodies] == ["mock", "live"]
    assert server.step_batch_sizes == ["1", "1", "1", "4", "4", "4"]


def test_pressure_trial_uses_worker_tenants_for_parallel_live_pressure(capsys):
    server = FakePressureTrialServer()
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(
            ["--confirm-live", "--stages", "4x2", "--workers", "2"],
            environ=LIVE_ENV,
            client=client,
        )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "workers=2" in captured.out
    assert set(server.reset_tenants[-2:]) == {
        "pressure-trial-smoke-w1",
        "pressure-trial-smoke-w2",
    }
    assert set(server.live_step_tenants) == {
        "pressure-trial-smoke-w1",
        "pressure-trial-smoke-w2",
    }
    assert server.live_step_count == 4


def test_pressure_trial_writes_json_report(tmp_path, capsys):
    report_path = tmp_path / "pressure-report.json"
    server = FakePressureTrialServer()
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(
            [
                "--confirm-live",
                "--stages",
                "1x2",
                "--report-json",
                str(report_path),
            ],
            environ=LIVE_ENV,
            client=client,
        )

    capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["workers"] == 1
    assert payload["mock"]["posted"] == 1
    assert payload["live"]["total_events_requested"] == 2
    assert payload["live"]["total_posted"] == 2
    assert payload["live"]["stages"][0]["latency_ms"]["p95"] >= 0
    assert payload["live"]["stages"][0]["batches"] == [
        {
            "request": 1,
            "tenant": "pressure-trial-smoke",
            "generated": 2,
            "posted": 2,
            "failed": 0,
            "latency_ms": payload["live"]["stages"][0]["batches"][0]["latency_ms"],
        }
    ]
    assert payload["live"]["stages"][0]["batches"][0]["latency_ms"] >= 0


def test_pressure_trial_timeout_is_operator_configurable():
    args = parse_args(["--confirm-live", "--timeout-seconds", "120"])

    config = pressure_config_from_env_and_args(args, LIVE_ENV)

    assert config.timeout_seconds == 120.0


def test_pressure_trial_watch_prints_batch_progress(capsys):
    server = FakePressureTrialServer()
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(
            ["--confirm-live", "--stages", "1x2", "--watch"],
            environ=LIVE_ENV,
            client=client,
        )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "batch stage=1" in captured.out
    assert "batch_size=2" in captured.out
    assert "posted=2" in captured.out


def test_pressure_trial_aborts_after_max_failures(capsys):
    server = FakePressureTrialServer(fail_live_batches_after=2)
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(
            ["--confirm-live", "--stages", "3x2", "--max-failures", "0"],
            environ=LIVE_ENV,
            client=client,
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "max failures" in captured.err.lower()


def test_pressure_trial_aborts_after_error_rate_gate(capsys):
    server = FakePressureTrialServer(fail_live_batches_after=1)
    with httpx.Client(
        base_url=LIVE_ENV["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        exit_code = main(
            [
                "--confirm-live",
                "--stages",
                "1x2",
                "--max-failures",
                "10",
                "--max-error-rate",
                "0",
            ],
            environ=LIVE_ENV,
            client=client,
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error rate" in captured.err.lower()


class FakePressureTrialServer:
    def __init__(self, *, fail_live_batches_after: int | None = None) -> None:
        self.lock = Lock()
        self.requests: list[httpx.Request] = []
        self.reset_bodies: list[dict] = []
        self.reset_tenants: list[str | None] = []
        self.current_mode = "mock"
        self.live_step_count = 0
        self.live_step_tenants: list[str | None] = []
        self.step_batch_sizes: list[str | None] = []
        self.fail_live_batches_after = fail_live_batches_after

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            path = request.url.path
            if path == "/api/simulate/stop":
                return httpx.Response(200, json={"running": False})
            if path == "/api/simulate/reset":
                body = decode_json(request)
                self.reset_bodies.append(body)
                self.reset_tenants.append(request.headers.get("x-regengine-tenant"))
                self.current_mode = body["delivery"]["mode"]
                return httpx.Response(200, json={"status": "reset"})
            if path == "/api/simulate/step":
                batch_size = request.url.params.get("batch_size")
                self.step_batch_sizes.append(batch_size)
                generated = int(batch_size or "1")
                if self.current_mode == "live":
                    self.live_step_count += 1
                    self.live_step_tenants.append(request.headers.get("x-regengine-tenant"))
                    if self.fail_live_batches_after is not None and self.live_step_count >= self.fail_live_batches_after:
                        return httpx.Response(
                            200,
                            json={
                                "generated": generated,
                                "posted": 0,
                                "failed": generated,
                                "lot_codes": ["TLC-PRESSURE-001"],
                                "delivery_status": "failed",
                                "delivery_mode": "live",
                                "delivery_attempts": 1,
                                "response": {},
                                "error": "downstream rejected batch",
                            },
                        )
                return httpx.Response(
                    200,
                    json={
                        "generated": generated,
                        "posted": generated,
                        "failed": 0,
                        "lot_codes": ["TLC-PRESSURE-001"],
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

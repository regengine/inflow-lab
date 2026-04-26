from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx


DEFAULT_TIMEOUT_SECONDS = 30.0
TRIAL_SCENARIO = "fresh_cut_processor"


class LiveTrialFailure(AssertionError):
    pass


@dataclass(frozen=True)
class LiveTrialConfig:
    base_url: str
    username: str
    password: str = field(repr=False)
    demo_tenant: str
    live_endpoint: str | None = None
    live_api_key: str | None = field(default=None, repr=False)
    live_tenant_id: str | None = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def redact(self, value: str) -> str:
        redacted = value
        for secret in secret_values(self.password, self.live_api_key, self.live_tenant_id):
            redacted = redacted.replace(secret, "[redacted]")
        return redacted


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> int:
    try:
        args = parse_args(argv)
        validate_requested_mode(args)
        config = config_from_env(environ, require_live=args.confirm_live)
        summary = run_live_trial(config, confirm_live=args.confirm_live, client=client)
    except LiveTrialFailure as exc:
        print(f"Live trial failed: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Live trial failed: HTTP client error: {exc}", file=sys.stderr)
        return 1

    if args.confirm_live:
        print(
            "Live trial completed: "
            f"base_url={config.base_url}, "
            f"demo_tenant={config.demo_tenant}, "
            f"mock_posted={summary['mock_posted']}, "
            f"live_posted={summary['live_posted']}, "
            f"live_failed={summary['live_failed']}, "
            f"live_delivery_status={summary['live_delivery_status']}"
        )
        return 0 if summary["live_failed"] == 0 else 1

    print(
        "Live trial dry-run passed: "
        f"base_url={config.base_url}, "
        f"demo_tenant={config.demo_tenant}, "
        f"mock_posted={summary['mock_posted']}, "
        f"mock_failed={summary['mock_failed']}"
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a gated RegEngine live-ingest trial through a deployed demo instance."
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="After the mock dry-run succeeds, send exactly one live batch.",
    )
    parser.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Run the mock dry-run only and do not require live credentials.",
    )
    return parser.parse_args(argv)


def validate_requested_mode(args: argparse.Namespace) -> None:
    if args.confirm_live and args.dry_run_only:
        raise LiveTrialFailure("Choose either --confirm-live or --dry-run-only, not both.")
    if not args.confirm_live and not args.dry_run_only:
        raise LiveTrialFailure(
            "Refusing to run. Pass --dry-run-only for a mock dry run or --confirm-live "
            "to send exactly one live batch."
        )


def config_from_env(
    environ: dict[str, str] | None = None,
    *,
    require_live: bool,
) -> LiveTrialConfig:
    environ = environ or os.environ
    required_names = [
        "REGENGINE_REMOTE_BASE_URL",
        "REGENGINE_REMOTE_USERNAME",
        "REGENGINE_REMOTE_PASSWORD",
        "REGENGINE_REMOTE_TENANT",
    ]
    if require_live:
        required_names.extend(
            [
                "REGENGINE_LIVE_ENDPOINT",
                "REGENGINE_LIVE_API_KEY",
                "REGENGINE_LIVE_TENANT_ID",
            ]
        )

    missing = [name for name in required_names if not environ.get(name)]
    if missing:
        raise LiveTrialFailure(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return LiveTrialConfig(
        base_url=normalize_base_url(environ["REGENGINE_REMOTE_BASE_URL"]),
        username=environ["REGENGINE_REMOTE_USERNAME"],
        password=environ["REGENGINE_REMOTE_PASSWORD"],
        demo_tenant=environ["REGENGINE_REMOTE_TENANT"],
        live_endpoint=normalize_base_url(environ["REGENGINE_LIVE_ENDPOINT"])
        if environ.get("REGENGINE_LIVE_ENDPOINT")
        else None,
        live_api_key=environ.get("REGENGINE_LIVE_API_KEY"),
        live_tenant_id=environ.get("REGENGINE_LIVE_TENANT_ID"),
    )


def run_live_trial(
    config: LiveTrialConfig,
    *,
    confirm_live: bool,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=config.base_url,
            follow_redirects=True,
            timeout=config.timeout_seconds,
            verify=True,
        )

    try:
        mock_step = run_mock_dry_run(client, config)
        summary = {
            "demo_tenant": config.demo_tenant,
            "mock_posted": mock_step.get("posted", 0),
            "mock_failed": mock_step.get("failed", 0),
            "mock_delivery_status": mock_step.get("delivery_status"),
        }

        if confirm_live:
            live_step = run_one_live_batch(client, config)
            summary.update(
                {
                    "live_posted": live_step.get("posted", 0),
                    "live_failed": live_step.get("failed", 0),
                    "live_delivery_status": live_step.get("delivery_status"),
                    "live_delivery_attempts": live_step.get("delivery_attempts", 0),
                }
            )
        return summary
    finally:
        stop_simulation(client, config)


def run_mock_dry_run(client: httpx.Client, config: LiveTrialConfig) -> dict[str, Any]:
    stop_simulation(client, config)
    request_json(
        client,
        config,
        "POST",
        "/api/simulate/reset",
        json={
            "source": "live-trial-dry-run",
            "scenario": TRIAL_SCENARIO,
            "batch_size": 1,
            "seed": 204,
            "delivery": {"mode": "mock"},
        },
    )
    step = request_json(
        client,
        config,
        "POST",
        "/api/simulate/step",
        params={"batch_size": "1"},
    )
    assert_equal(step.get("generated"), 1, "mock dry-run generated count")
    assert_equal(step.get("delivery_mode"), "mock", "mock dry-run delivery mode")
    if step.get("failed", 0) != 0:
        raise LiveTrialFailure(
            f"Mock dry-run failed: posted={step.get('posted', 0)} failed={step.get('failed', 0)}"
        )
    return step


def run_one_live_batch(client: httpx.Client, config: LiveTrialConfig) -> dict[str, Any]:
    if not (config.live_endpoint and config.live_api_key and config.live_tenant_id):
        raise LiveTrialFailure("Live endpoint, API key, and tenant id are required for --confirm-live.")

    request_json(
        client,
        config,
        "POST",
        "/api/simulate/reset",
        json={
            "source": "live-trial",
            "scenario": TRIAL_SCENARIO,
            "batch_size": 1,
            "seed": 204,
            "delivery": {
                "mode": "live",
                "endpoint": config.live_endpoint,
                "api_key": config.live_api_key,
                "tenant_id": config.live_tenant_id,
            },
        },
    )
    step = request_json(
        client,
        config,
        "POST",
        "/api/simulate/step",
        params={"batch_size": "1"},
    )
    assert_equal(step.get("generated"), 1, "live batch generated count")
    assert_equal(step.get("delivery_mode"), "live", "live batch delivery mode")
    assert_equal(step.get("delivery_attempts"), 1, "live batch delivery attempts")
    return step


def stop_simulation(client: httpx.Client, config: LiveTrialConfig) -> None:
    try:
        response = request(client, config, "POST", "/api/simulate/stop")
    except httpx.HTTPError:
        return
    if response.status_code >= 500:
        raise LiveTrialFailure(
            "Failed to stop simulation loop: "
            f"HTTP {response.status_code}: {config.redact(response.text[:300])}"
        )


def request_json(
    client: httpx.Client,
    config: LiveTrialConfig,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = request(client, config, method, path, json=json, params=params)
    assert_status(config, response, 200, path)
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveTrialFailure(
            f"{path}: expected JSON response, got {config.redact(response.text[:300])!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise LiveTrialFailure(f"{path}: expected JSON object response")
    return payload


def request(
    client: httpx.Client,
    config: LiveTrialConfig,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    return client.request(
        method,
        path,
        headers={"X-RegEngine-Tenant": config.demo_tenant},
        json=json,
        params=params,
        auth=httpx.BasicAuth(config.username, config.password),
    )


def assert_status(
    config: LiveTrialConfig,
    response: httpx.Response,
    expected_status: int,
    label: str,
) -> None:
    if response.status_code != expected_status:
        raise LiveTrialFailure(
            f"{label}: expected HTTP {expected_status}, got "
            f"{response.status_code}: {config.redact(response.text[:500])}"
        )


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise LiveTrialFailure(f"{label}: expected {expected!r}, got {actual!r}")


def normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LiveTrialFailure(f"Expected an HTTP(S) URL, got {value!r}")
    return base_url


def secret_values(*extra_values: str | None) -> set[str]:
    values = {value for value in extra_values if value}
    for key, value in os.environ.items():
        key_lower = key.lower()
        credential_name = any(
            token in key_lower
            for token in ("password", "api_key", "apikey", "secret", "token")
        )
        if value and credential_name:
            values.add(value)
    return {value for value in values if len(value) >= 4}


if __name__ == "__main__":
    raise SystemExit(main())

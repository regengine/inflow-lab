from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imported after the sys.path bootstrap above so `python scripts/live_trial.py`
# works from a clean checkout; hence the E402 waiver.
from scripts import _smoke_common as smoke  # noqa: E402
from scripts.remote_smoke import (  # noqa: E402
    ALLOWED_HOSTS_ENV,
    DEFAULT_ALLOWED_BASE_HOSTS,
)


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
        return smoke.redact_secrets(
            value,
            sorted(secret_values(self.password, self.live_api_key, self.live_tenant_id)),
        )


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
    environ: Mapping[str, str] | None = None,
    *,
    require_live: bool,
) -> LiveTrialConfig:
    env: Mapping[str, str] = environ if environ else os.environ
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

    missing = [name for name in required_names if not env.get(name)]
    if missing:
        raise LiveTrialFailure(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return LiveTrialConfig(
        # The demo base URL carries the shared-demo Basic Auth header on every
        # request, so it goes through the same host allowlist remote_smoke.py
        # uses. The live ingest endpoint never sees those credentials (its own
        # API key travels in the request body), so it only gets the scheme
        # check -- allowlisting it would pin the trial to one RegEngine host.
        base_url=normalize_base_url(env["REGENGINE_REMOTE_BASE_URL"], environ=env),
        username=env["REGENGINE_REMOTE_USERNAME"],
        password=env["REGENGINE_REMOTE_PASSWORD"],
        demo_tenant=env["REGENGINE_REMOTE_TENANT"],
        live_endpoint=normalize_live_endpoint(env["REGENGINE_LIVE_ENDPOINT"])
        if env.get("REGENGINE_LIVE_ENDPOINT")
        else None,
        live_api_key=env.get("REGENGINE_LIVE_API_KEY"),
        live_tenant_id=env.get("REGENGINE_LIVE_TENANT_ID"),
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

    # Set the moment we are about to arm live delivery, so the finally below
    # disarms even when the arming POST itself, the live step, or one of the
    # assertions after it raises. Without this the tenant controller keeps the
    # customer's endpoint + API key with delivery.mode=live for the lifetime of
    # the server process, and the next Start/Step click posts simulated CTEs
    # into their production ingest.
    armed_live = False

    try:
        mock_step = run_mock_dry_run(client, config)
        summary = {
            "demo_tenant": config.demo_tenant,
            "mock_posted": mock_step.get("posted", 0),
            "mock_failed": mock_step.get("failed", 0),
            "mock_delivery_status": mock_step.get("delivery_status"),
        }

        if confirm_live:
            require_live_credentials(config)
            armed_live = True
            arm_live_delivery(client, config)
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
        try:
            stop_simulation(client, config)
        finally:
            try:
                if armed_live:
                    disarm_live_delivery(client, config)
            finally:
                # A caller-supplied client stays open -- it owns its own lifetime.
                if owns_client:
                    client.close()


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


def require_live_credentials(config: LiveTrialConfig) -> None:
    if not (config.live_endpoint and config.live_api_key and config.live_tenant_id):
        raise LiveTrialFailure("Live endpoint, API key, and tenant id are required for --confirm-live.")


def arm_live_delivery(client: httpx.Client, config: LiveTrialConfig) -> None:
    """Point the demo tenant at the customer's live ingest for one batch.

    Always paired with :func:`disarm_live_delivery` in ``run_live_trial``'s
    ``finally``.
    """
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


def disarm_live_delivery(client: httpx.Client, config: LiveTrialConfig) -> None:
    """Reset the demo tenant back to mock delivery and prove it took effect.

    ``/api/simulate/reset`` replaces the controller's whole config, so a body
    carrying ``delivery: {"mode": "mock"}`` drops the stored endpoint, API key
    and tenant id along with the live mode -- ``DeliveryConfig`` defaults them
    all to ``None``. The read-back through ``/api/integration/status`` is the
    part that matters: it is the only endpoint that reports
    ``api_key_configured``, because ``/api/simulate/status`` sanitizes the key
    out of its response and so cannot distinguish a disarmed tenant from an
    armed one.
    """
    request_json(
        client,
        config,
        "POST",
        "/api/simulate/reset",
        json={
            "source": "live-trial-disarm",
            "scenario": TRIAL_SCENARIO,
            "batch_size": 1,
            "seed": 204,
            "delivery": {"mode": "mock"},
        },
    )
    integration = request_json(client, config, "GET", "/api/integration/status")
    mode = integration.get("mode")
    if mode != "mock":
        raise LiveTrialFailure(
            f"Delivery did not revert to mock for tenant {config.demo_tenant!r}: "
            f"/api/integration/status still reports mode={mode!r}. Disarm it by "
            "hand before anyone uses this demo."
        )
    if integration.get("api_key_configured") or integration.get("tenant_configured"):
        raise LiveTrialFailure(
            f"Live credentials are still configured for tenant {config.demo_tenant!r} "
            "after the revert. Disarm it by hand before anyone uses this demo."
        )
    print(f"Delivery reverted to mock for tenant {config.demo_tenant}.")


def run_one_live_batch(client: httpx.Client, config: LiveTrialConfig) -> dict[str, Any]:
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
    return smoke.response_json(
        response, path, failure=LiveTrialFailure, redact=config.redact
    )


def request(
    client: httpx.Client,
    config: LiveTrialConfig,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    return smoke.request(
        client,
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
    smoke.assert_status(
        response,
        expected_status,
        label,
        failure=LiveTrialFailure,
        redact=config.redact,
    )


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    smoke.assert_equal(actual, expected, label, failure=LiveTrialFailure)


def allowed_base_hosts(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Hosts the live trial may send the shared-demo Basic Auth secrets to.

    Shares remote_smoke.py's allowlist and REGENGINE_REMOTE_ALLOWED_HOSTS
    override, because it is the same demo instance and the same credentials.
    """
    return smoke.allowed_hosts_from_env(
        ALLOWED_HOSTS_ENV, DEFAULT_ALLOWED_BASE_HOSTS, environ
    )


def normalize_base_url(
    value: str,
    allowed_hosts: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    return smoke.normalize_base_url(
        value,
        allowed_hosts=(
            allowed_hosts if allowed_hosts is not None else allowed_base_hosts(environ)
        ),
        failure=LiveTrialFailure,
        scheme_error=f"Expected an HTTP(S) URL, got {value!r}",
        host_error=lambda host, hosts: (
            f"REGENGINE_REMOTE_BASE_URL host {host!r} is not allowed. The live "
            "trial attaches the shared-demo Basic Auth credentials to every "
            f"request, so it only runs against {', '.join(hosts)}. Set "
            f"{ALLOWED_HOSTS_ENV} to extend the allowlist."
        ),
    )


def normalize_live_endpoint(value: str) -> str:
    """Scheme-check the live ingest endpoint.

    Deliberately not host-allowlisted: the endpoint receives its own
    REGENGINE_LIVE_API_KEY in the request body and never the shared-demo Basic
    Auth header, and pinning it would stop the trial from targeting a
    customer's own RegEngine deployment.
    """
    return smoke.normalize_base_url(
        value,
        allowed_hosts=None,
        failure=LiveTrialFailure,
        scheme_error=f"Expected an HTTP(S) URL, got {value!r}",
    )


secret_values = smoke.secret_values


if __name__ == "__main__":
    raise SystemExit(main())

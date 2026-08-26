from __future__ import annotations

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

# Imported after the sys.path bootstrap above so `python scripts/remote_smoke.py`
# works from a clean checkout; hence the E402 waivers.
from app.build_info import APP_VERSION  # noqa: E402
from scripts import _smoke_common as smoke  # noqa: E402


DEFAULT_TENANT = "remote-smoke"
# Hosts remote smoke is allowed to send the shared-demo Basic Auth secrets to.
# A leading dot matches subdomains. `demo.example.com` is the RFC 2606 reserved
# documentation domain used by the docs and the offline test transport; it is
# not routable, so it cannot receive credentials.
DEFAULT_ALLOWED_BASE_HOSTS = (
    "regengine-inflow-lab-gh-production.up.railway.app",
    "demo.example.com",
)
ALLOWED_HOSTS_ENV = "REGENGINE_REMOTE_ALLOWED_HOSTS"
DEFAULT_UNTRUSTED_ORIGIN = "https://untrusted.example"
FRESH_CUT_OUTPUT_LOT = "TLC-DEMO-FC-OUT-001"


class RemoteSmokeFailure(AssertionError):
    pass


@dataclass(frozen=True)
class RemoteSmokeConfig:
    base_url: str
    username: str
    password: str = field(repr=False)
    tenant: str = DEFAULT_TENANT
    cors_origin: str | None = None
    untrusted_origin: str = DEFAULT_UNTRUSTED_ORIGIN
    expected_build_sha: str | None = None
    timeout_seconds: float = 30.0

    @property
    def allowed_origin(self) -> str:
        return self.cors_origin or origin_from_url(self.base_url)

    def redact(self, value: str) -> str:
        redacted = value
        for secret in secret_values(self.password):
            redacted = redacted.replace(secret, "[redacted]")
        return redacted


def main() -> int:
    try:
        config = config_from_env()
        summary = run_remote_smoke(config)
    except RemoteSmokeFailure as exc:
        print(f"Remote smoke failed: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Remote smoke failed: HTTP client error: {exc}", file=sys.stderr)
        return 1

    print(
        "Remote smoke passed: "
        f"base_url={config.base_url}, "
        f"tenant={summary['tenant']}, "
        f"fixture_stored={summary['fixture_stored']}, "
        f"fixture_posted={summary['fixture_posted']}, "
        f"lineage_records={summary['lineage_records']}, "
        f"epcis_events={summary['epcis_events']}, "
        f"build_version={summary['build_version']}, "
        f"build_commit={summary['build_commit'] or 'unknown'}"
    )
    return 0


def config_from_env(environ: Mapping[str, str] | None = None) -> RemoteSmokeConfig:
    env: Mapping[str, str] = environ if environ else os.environ
    missing = [
        name
        for name in (
            "REGENGINE_REMOTE_BASE_URL",
            "REGENGINE_REMOTE_USERNAME",
            "REGENGINE_REMOTE_PASSWORD",
        )
        if not env.get(name)
    ]
    if missing:
        raise RemoteSmokeFailure(
            "Missing required environment variables: " + ", ".join(missing)
        )

    base_url = normalize_base_url(
        env["REGENGINE_REMOTE_BASE_URL"],
        allowed_hosts=allowed_base_hosts(env),
    )
    return RemoteSmokeConfig(
        base_url=base_url,
        username=env["REGENGINE_REMOTE_USERNAME"],
        password=env["REGENGINE_REMOTE_PASSWORD"],
        tenant=env.get("REGENGINE_REMOTE_TENANT") or DEFAULT_TENANT,
        cors_origin=normalize_optional_origin(env.get("REGENGINE_REMOTE_CORS_ORIGIN")),
        untrusted_origin=normalize_optional_origin(
            env.get("REGENGINE_REMOTE_UNTRUSTED_ORIGIN")
        )
        or DEFAULT_UNTRUSTED_ORIGIN,
        expected_build_sha=env.get("REGENGINE_EXPECTED_BUILD_SHA") or None,
    )


def run_remote_smoke(
    config: RemoteSmokeConfig,
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
        healthz = request_json(client, config, "GET", "/api/healthz", authenticated=False)
        assert_equal(healthz.get("ok"), True, "healthz ok")
        build = assert_build_info(config, healthz.get("build"))
        if config.expected_build_sha:
            assert_build_sha(
                actual=build.get("commit_sha"),
                expected=config.expected_build_sha,
                label="healthz build commit",
            )

        unauthenticated_health = client.get("/api/health")
        assert_status(config, unauthenticated_health, 401, "Basic Auth enforcement")

        health = request_json(
            client,
            config,
            "GET",
            "/api/health",
            headers={"Origin": config.allowed_origin},
        )
        assert_equal(health.get("tenant"), config.tenant, "health tenant")
        auth = health.get("auth") or {}
        assert_equal(auth.get("enabled"), True, "health auth enabled")
        assert_equal(auth.get("uses_default_storage"), False, "health tenant storage")
        assert_header(
            config,
            health_response_header(client, config, config.allowed_origin),
            "access-control-allow-origin",
            config.allowed_origin,
            "allowed CORS origin",
        )

        blocked_cors = request(
            client,
            config,
            "GET",
            "/api/health",
            headers={"Origin": config.untrusted_origin},
        )
        assert_status(config, blocked_cors, 200, "blocked CORS health probe")
        blocked_origin = blocked_cors.headers.get("access-control-allow-origin")
        if blocked_origin == config.untrusted_origin:
            raise RemoteSmokeFailure(
                "blocked CORS origin: untrusted origin was allowed"
            )

        request_json(
            client,
            config,
            "POST",
            "/api/simulate/reset",
            json={
                "scenario": "fresh_cut_processor",
                "batch_size": 1,
                "seed": 204,
                "delivery": {"mode": "mock"},
            },
        )

        fixture = request_json(
            client,
            config,
            "POST",
            "/api/demo-fixtures/fresh_cut_transformation/load",
            json={
                "reset": True,
                "source": "remote-smoke",
                "delivery": {"mode": "mock"},
            },
        )
        assert_equal(fixture.get("status"), "loaded", "fixture load status")
        assert_equal(fixture.get("stored"), 13, "fixture stored events")
        assert_equal(fixture.get("posted"), 13, "fixture posted events")
        assert_equal(fixture.get("failed"), 0, "fixture failed events")
        assert_equal(fixture.get("delivery_mode"), "mock", "fixture delivery mode")

        lineage = request_json(
            client,
            config,
            "GET",
            f"/api/lineage/{FRESH_CUT_OUTPUT_LOT}",
        )
        records = lineage.get("records") or []
        lot_codes = {
            record.get("event", {}).get("traceability_lot_code") for record in records
        }
        assert_in("TLC-DEMO-FC-HARVEST-001", lot_codes, "lineage harvest lot")
        assert_in("TLC-DEMO-FC-PACK-001", lot_codes, "lineage packed input lot")
        assert_in(FRESH_CUT_OUTPUT_LOT, lot_codes, "lineage output lot")

        fda_response = request(
            client,
            config,
            "GET",
            "/api/mock/regengine/export/fda-request",
            params={
                "preset": "lot_trace",
                "traceability_lot_code": FRESH_CUT_OUTPUT_LOT,
            },
        )
        assert_status(config, fda_response, 200, "FDA lot-trace export")
        assert_in(
            "BATCH-DEMO-FC-001",
            fda_response.text,
            "FDA lot-trace batch reference",
        )

        epcis = request_json(
            client,
            config,
            "GET",
            "/api/mock/regengine/export/epcis",
            params={"traceability_lot_code": FRESH_CUT_OUTPUT_LOT},
        )
        epcis_events = epcis.get("epcisBody", {}).get("eventList") or []
        event_types = {event.get("type") for event in epcis_events}
        assert_in("TransformationEvent", event_types, "EPCIS transformation event")

        return {
            "tenant": config.tenant,
            "fixture_stored": fixture["stored"],
            "fixture_posted": fixture["posted"],
            "lineage_records": len(records),
            "epcis_events": len(epcis_events),
            "build_version": build.get("version"),
            "build_commit": build.get("commit_sha_short"),
        }
    finally:
        if owns_client:
            client.close()


def request_json(
    client: httpx.Client,
    config: RemoteSmokeConfig,
    method: str,
    path: str,
    *,
    authenticated: bool = True,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = request(
        client,
        config,
        method,
        path,
        authenticated=authenticated,
        headers=headers,
        json=json,
        params=params,
    )
    assert_status(config, response, 200, path)
    return smoke.response_json(
        response, path, failure=RemoteSmokeFailure, redact=config.redact
    )


def request(
    client: httpx.Client,
    config: RemoteSmokeConfig,
    method: str,
    path: str,
    *,
    authenticated: bool = True,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = {"X-RegEngine-Tenant": config.tenant}
    request_headers.update(headers or {})
    return smoke.request(
        client,
        method,
        path,
        headers=request_headers,
        json=json,
        params=params,
        auth=httpx.BasicAuth(config.username, config.password) if authenticated else None,
    )


def health_response_header(
    client: httpx.Client,
    config: RemoteSmokeConfig,
    origin: str,
) -> httpx.Response:
    return request(
        client,
        config,
        "GET",
        "/api/health",
        headers={"Origin": origin},
    )


def assert_status(
    config: RemoteSmokeConfig,
    response: httpx.Response,
    expected_status: int,
    label: str,
) -> None:
    smoke.assert_status(
        response,
        expected_status,
        label,
        failure=RemoteSmokeFailure,
        redact=config.redact,
    )


def assert_header(
    config: RemoteSmokeConfig,
    response: httpx.Response,
    header_name: str,
    expected_value: str,
    label: str,
) -> None:
    smoke.assert_header(
        response,
        header_name,
        expected_value,
        label,
        failure=RemoteSmokeFailure,
        redact=config.redact,
    )


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    smoke.assert_equal(actual, expected, label, failure=RemoteSmokeFailure)


def assert_in(member: Any, container: Any, label: str) -> None:
    smoke.assert_in(member, container, label, failure=RemoteSmokeFailure)


def assert_build_info(config: RemoteSmokeConfig, build: Any) -> dict[str, Any]:
    return smoke.assert_build_info(build, APP_VERSION, failure=RemoteSmokeFailure)


def assert_build_sha(actual: Any, expected: str, label: str) -> None:
    smoke.assert_build_sha(actual, expected, label, failure=RemoteSmokeFailure)


def allowed_base_hosts(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Hosts remote smoke may send the shared-demo Basic Auth secrets to.

    The workflow_dispatch `base_url` input is free text, so without this the
    dispatcher could point the run at a host they control and collect the live
    REGENGINE_REMOTE_USERNAME/PASSWORD out of the Basic Auth header. Override
    with REGENGINE_REMOTE_ALLOWED_HOSTS (comma separated, a leading dot matches
    subdomains) -- an env var, so it cannot be set from a dispatch input.
    """
    return smoke.allowed_hosts_from_env(
        ALLOWED_HOSTS_ENV, DEFAULT_ALLOWED_BASE_HOSTS, environ
    )


def normalize_base_url(value: str, allowed_hosts: Sequence[str] | None = None) -> str:
    return smoke.normalize_base_url(
        value,
        allowed_hosts=allowed_hosts if allowed_hosts is not None else allowed_base_hosts(),
        failure=RemoteSmokeFailure,
        scheme_error=(
            "REGENGINE_REMOTE_BASE_URL must be an HTTP(S) URL such as "
            "https://demo.example.com"
        ),
        host_error=lambda host, hosts: (
            f"REGENGINE_REMOTE_BASE_URL host {host!r} is not allowed. Remote smoke "
            "attaches the shared-demo Basic Auth credentials to every "
            "authenticated request, so it only runs against "
            f"{', '.join(hosts)}. Set {ALLOWED_HOSTS_ENV} to extend the allowlist."
        ),
    )


def normalize_optional_origin(value: str | None) -> str | None:
    return smoke.normalize_optional_origin(
        value,
        failure=RemoteSmokeFailure,
        error=(
            "Remote smoke CORS origins must be HTTP(S) origins such as "
            "https://demo.example.com"
        ),
    )


origin_from_url = smoke.origin_from_url
secret_values = smoke.secret_values
_sha_prefix_match = smoke.sha_prefix_match


if __name__ == "__main__":
    raise SystemExit(main())

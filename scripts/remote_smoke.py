from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.build_info import APP_VERSION
from scripts import _smoke_common
from scripts._smoke_common import origin_from_url, secret_values


DEFAULT_TENANT = "remote-smoke"
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


def config_from_env(environ: dict[str, str] | None = None) -> RemoteSmokeConfig:
    environ = environ or os.environ
    missing = [
        name
        for name in (
            "REGENGINE_REMOTE_BASE_URL",
            "REGENGINE_REMOTE_USERNAME",
            "REGENGINE_REMOTE_PASSWORD",
        )
        if not environ.get(name)
    ]
    if missing:
        raise RemoteSmokeFailure(
            "Missing required environment variables: " + ", ".join(missing)
        )

    base_url = normalize_base_url(environ["REGENGINE_REMOTE_BASE_URL"])
    return RemoteSmokeConfig(
        base_url=base_url,
        username=environ["REGENGINE_REMOTE_USERNAME"],
        password=environ["REGENGINE_REMOTE_PASSWORD"],
        tenant=environ.get("REGENGINE_REMOTE_TENANT") or DEFAULT_TENANT,
        cors_origin=normalize_optional_origin(environ.get("REGENGINE_REMOTE_CORS_ORIGIN")),
        untrusted_origin=normalize_optional_origin(
            environ.get("REGENGINE_REMOTE_UNTRUSTED_ORIGIN")
        )
        or DEFAULT_UNTRUSTED_ORIGIN,
        expected_build_sha=environ.get("REGENGINE_EXPECTED_BUILD_SHA") or None,
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
    return _smoke_common.response_json(
        RemoteSmokeFailure, response, path, redact=config.redact
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
    auth = httpx.BasicAuth(config.username, config.password) if authenticated else None
    return client.request(
        method,
        path,
        headers=request_headers,
        json=json,
        params=params,
        auth=auth,
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


# The shared harness in _smoke_common takes the failure type as its first
# argument so every script can keep its own exception class. These bind
# this script's class once instead of at each of the call sites below.
assert_equal = partial(_smoke_common.assert_equal, RemoteSmokeFailure)
assert_in = partial(_smoke_common.assert_in, RemoteSmokeFailure)
assert_build_sha = partial(_smoke_common.assert_build_sha, RemoteSmokeFailure)


def assert_status(
    config: RemoteSmokeConfig,
    response: httpx.Response,
    expected_status: int,
    label: str,
) -> None:
    _smoke_common.assert_status(
        RemoteSmokeFailure, response, expected_status, label, redact=config.redact
    )


def assert_header(
    config: RemoteSmokeConfig,
    response: httpx.Response,
    header_name: str,
    expected_value: str,
    label: str,
) -> None:
    _smoke_common.assert_header(
        RemoteSmokeFailure,
        response,
        header_name,
        expected_value,
        label,
        redact=config.redact,
    )


def assert_build_info(config: RemoteSmokeConfig, build: Any) -> dict[str, Any]:
    return _smoke_common.assert_build_info(RemoteSmokeFailure, build, APP_VERSION)


def normalize_base_url(value: str) -> str:
    return _smoke_common.normalize_base_url(
        RemoteSmokeFailure,
        value,
        message=(
            "REGENGINE_REMOTE_BASE_URL must be an HTTP(S) URL such as "
            "https://demo.example.com"
        ),
    )


def normalize_optional_origin(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    origin = value.strip().rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in _smoke_common.HTTP_SCHEMES or not parsed.netloc:
        raise RemoteSmokeFailure(
            "Remote smoke CORS origins must be HTTP(S) origins such as "
            "https://demo.example.com"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx


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
        f"epcis_events={summary['epcis_events']}"
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
    try:
        payload = response.json()
    except ValueError as exc:
        raise RemoteSmokeFailure(
            f"{path}: expected JSON response, got "
            f"{config.redact(response.text[:300])!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise RemoteSmokeFailure(f"{path}: expected JSON object response")
    return payload


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


def assert_status(
    config: RemoteSmokeConfig,
    response: httpx.Response,
    expected_status: int,
    label: str,
) -> None:
    if response.status_code != expected_status:
        body = config.redact(response.text[:500])
        raise RemoteSmokeFailure(
            f"{label}: expected HTTP {expected_status}, got "
            f"{response.status_code}: {body}"
        )


def assert_header(
    config: RemoteSmokeConfig,
    response: httpx.Response,
    header_name: str,
    expected_value: str,
    label: str,
) -> None:
    actual = response.headers.get(header_name)
    if actual != expected_value:
        raise RemoteSmokeFailure(
            f"{label}: expected {header_name}={expected_value!r}, got "
            f"{config.redact(str(actual))!r}"
        )


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise RemoteSmokeFailure(f"{label}: expected {expected!r}, got {actual!r}")


def assert_in(member: Any, container: Any, label: str) -> None:
    if member not in container:
        raise RemoteSmokeFailure(f"{label}: expected {member!r} to be present")


def normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RemoteSmokeFailure(
            "REGENGINE_REMOTE_BASE_URL must be an HTTP(S) URL such as "
            "https://demo.example.com"
        )
    return base_url


def normalize_optional_origin(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    origin = value.strip().rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RemoteSmokeFailure(
            "Remote smoke CORS origins must be HTTP(S) origins such as "
            "https://demo.example.com"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def origin_from_url(value: str) -> str:
    parsed = urlparse(value)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def secret_values(*extra_values: str | None) -> set[str]:
    values = {value for value in extra_values if value}
    for key, value in os.environ.items():
        key_lower = key.lower()
        if value and any(token in key_lower for token in ("password", "api_key", "apikey", "secret", "token")):
            values.add(value)
    return {value for value in values if len(value) >= 4}


if __name__ == "__main__":
    raise SystemExit(main())

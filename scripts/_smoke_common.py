"""Shared HTTP smoke-test harness for the scripts in this directory.

`remote_smoke.py`, `live_trial.py`, `smoke_regression.py` and
`run_full_fsma_simulation.py` had each grown its own copy of the same small
assertion harness -- `request_json`'s status/JSON/redaction handling,
`assert_status`, `assert_equal`, `assert_in`, secret redaction and base-URL
normalization -- and `browser_smoke.py` carried `remote_smoke.py`'s SHA
comparison byte for byte. Two of those scripts run in CI and one is the
documented manual release gate, so a correctness fix applied to one copy
was silently missing from the rest (#139).

Every helper here takes the caller's failure type as its first argument
rather than raising one of its own, so each script keeps the exception
class its `main()` and its tests already catch. The scripts keep thin
wrappers where a config-shaped signature reads better at the call site;
what none of them keeps is a second implementation.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx


# Environment variable names containing any of these are treated as
# carrying a credential, and their values are redacted out of anything a
# script prints.
CREDENTIAL_NAME_TOKENS = ("password", "api_key", "apikey", "secret", "token")

# Values shorter than this are too generic to redact usefully -- blanking
# every occurrence of a 2-character value would mangle unrelated output.
MIN_REDACTED_LENGTH = 4

HTTP_SCHEMES = frozenset({"http", "https"})

Redactor = Callable[[str], str]


def secret_values(
    *extra_values: str | None,
    environ: Mapping[str, str] | None = None,
) -> set[str]:
    """Every value worth blanking out of printed output."""
    values = {value for value in extra_values if value}
    for key, value in (os.environ if environ is None else environ).items():
        if value and any(token in key.lower() for token in CREDENTIAL_NAME_TOKENS):
            values.add(value)
    return {value for value in values if len(value) >= MIN_REDACTED_LENGTH}


def _redacted(redact: Redactor | None, value: str) -> str:
    return value if redact is None else redact(value)


def normalize_base_url(
    failure: type[BaseException],
    value: str,
    *,
    message: str | None = None,
) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in HTTP_SCHEMES or not parsed.netloc:
        raise failure(message or f"Expected an HTTP(S) URL, got {value!r}")
    return base_url


def origin_from_url(value: str) -> str:
    parsed = urlparse(value)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def assert_equal(failure: type[BaseException], actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise failure(f"{label}: expected {expected!r}, got {actual!r}")


def assert_in(failure: type[BaseException], member: Any, container: Any, label: str) -> None:
    if member not in container:
        raise failure(f"{label}: expected {member!r} to be present")


def assert_status(
    failure: type[BaseException],
    response: httpx.Response,
    expected_status: int,
    label: str = "response",
    *,
    redact: Redactor | None = None,
) -> None:
    if response.status_code != expected_status:
        body = _redacted(redact, response.text[:500])
        raise failure(
            f"{label}: expected HTTP {expected_status}, got {response.status_code}: {body}"
        )


def assert_header(
    failure: type[BaseException],
    response: httpx.Response,
    header_name: str,
    expected_value: str,
    label: str,
    *,
    redact: Redactor | None = None,
) -> None:
    actual = response.headers.get(header_name)
    if actual != expected_value:
        raise failure(
            f"{label}: expected {header_name}={expected_value!r}, got "
            f"{_redacted(redact, str(actual))!r}"
        )


def response_json(
    failure: type[BaseException],
    response: httpx.Response,
    label: str,
    *,
    expected_status: int = 200,
    redact: Redactor | None = None,
) -> dict[str, Any]:
    """Assert the status, then decode a JSON object body.

    Both failure modes are reported with the response text redacted, since
    an error body from an authenticated endpoint can echo a credential
    back.
    """
    assert_status(failure, response, expected_status, label, redact=redact)
    try:
        payload = response.json()
    except ValueError as exc:
        raise failure(
            f"{label}: expected JSON response, got {_redacted(redact, response.text[:300])!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise failure(f"{label}: expected JSON object response")
    return payload


def assert_build_info(
    failure: type[BaseException],
    build: Any,
    expected_version: str,
) -> dict[str, Any]:
    if not isinstance(build, dict):
        raise failure("healthz build: expected build metadata object")
    assert_equal(failure, build.get("version"), expected_version, "healthz build version")
    for name in ("commit_sha", "commit_sha_short", "branch", "deployment_id"):
        value = build.get(name)
        if value is not None and not isinstance(value, str):
            raise failure(f"healthz build {name}: expected string or null")
    return build


def assert_build_sha(
    failure: type[BaseException],
    actual: Any,
    expected: str,
    label: str,
) -> None:
    if not isinstance(actual, str) or not actual:
        raise failure(f"{label}: expected deployed commit {expected[:12]}, got none")
    if not sha_prefix_match(actual, expected):
        raise failure(f"{label}: expected deployed commit {expected[:12]}, got {actual[:12]}")


def sha_prefix_match(actual: str, expected: str) -> bool:
    """True when either commit SHA is a prefix of the other.

    Deployments report a full SHA and workflows sometimes carry a short
    one, so neither side can be assumed to be the longer of the two.
    """
    actual = actual.strip().lower()
    expected = expected.strip().lower()
    return actual.startswith(expected) or expected.startswith(actual)

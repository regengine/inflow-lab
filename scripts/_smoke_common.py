"""Shared HTTP smoke-test harness for the scripts in this directory.

``remote_smoke.py``, ``live_trial.py``, ``browser_smoke.py`` and
``smoke_regression.py`` used to carry four independent copies of the same
assertion helpers, secret redaction, base-URL normalization and build-metadata
checks. A fix to one copy was silently missing from the other three, which
matters because three of the four are CI-facing and the fourth is the
documented manual pre-release check.

Everything here is parameterized by the caller's failure class so each script
keeps raising (and catching) its own exception type, exactly as before.

Security note: :func:`normalize_base_url` carries the base-URL host allowlist.
The remote-smoke workflows take ``base_url`` as free-text ``workflow_dispatch``
input and attach the shared-demo Basic Auth secrets to every authenticated
request, so without the allowlist a dispatcher could point a run at a host they
control and collect those credentials out of the Authorization header. Do not
loosen it, and do not add a second copy of it elsewhere.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

__all__ = [
    "SmokeFailure",
    "Redactor",
    "allowed_hosts_from_env",
    "assert_build_info",
    "assert_build_sha",
    "assert_equal",
    "assert_header",
    "assert_in",
    "assert_status",
    "host_allowed",
    "normalize_base_url",
    "normalize_optional_origin",
    "origin_from_url",
    "request",
    "request_json",
    "response_json",
    "secret_values",
    "sha_prefix_match",
]


class SmokeFailure(AssertionError):
    """Base class for every smoke script's assertion failure."""


# A callable that scrubs known secrets out of a response body before it is
# printed. Scripts pass their config's ``redact`` bound method.
Redactor = Callable[[str], str]

#: Env-var name substrings that mark a value as a secret worth redacting.
SECRET_NAME_TOKENS = ("password", "api_key", "apikey", "secret", "token")

#: Values shorter than this are too generic to redact usefully (and redacting
#: them would mangle unrelated output).
MIN_SECRET_LENGTH = 4


def _identity(value: str) -> str:
    return value


def _scrub(redact: Redactor | None, value: str) -> str:
    return (redact or _identity)(value)


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------


def assert_equal(
    actual: Any,
    expected: Any,
    label: str,
    *,
    failure: type[Exception] = SmokeFailure,
) -> None:
    if actual != expected:
        raise failure(f"{label}: expected {expected!r}, got {actual!r}")


def assert_in(
    member: Any,
    container: Any,
    label: str,
    *,
    failure: type[Exception] = SmokeFailure,
) -> None:
    if member not in container:
        raise failure(f"{label}: expected {member!r} to be present")


def assert_status(
    response: httpx.Response,
    expected_status: int,
    label: str,
    *,
    failure: type[Exception] = SmokeFailure,
    redact: Redactor | None = None,
    body_chars: int = 500,
) -> None:
    if response.status_code != expected_status:
        body = _scrub(redact, response.text[:body_chars])
        raise failure(
            f"{label}: expected HTTP {expected_status}, got "
            f"{response.status_code}: {body}"
        )


def assert_header(
    response: httpx.Response,
    header_name: str,
    expected_value: str,
    label: str,
    *,
    failure: type[Exception] = SmokeFailure,
    redact: Redactor | None = None,
) -> None:
    actual = response.headers.get(header_name)
    if actual != expected_value:
        raise failure(
            f"{label}: expected {header_name}={expected_value!r}, got "
            f"{_scrub(redact, str(actual))!r}"
        )


def response_json(
    response: httpx.Response,
    label: str,
    *,
    failure: type[Exception] = SmokeFailure,
    redact: Redactor | None = None,
) -> dict[str, Any]:
    """Decode a JSON *object* body, or raise ``failure`` with a redacted body."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise failure(
            f"{label}: expected JSON response, got "
            f"{_scrub(redact, response.text[:300])!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise failure(f"{label}: expected JSON object response")
    return payload


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


def request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    auth: httpx.Auth | None = None,
    json: dict[str, Any] | None = None,
    params: Mapping[str, str] | None = None,
) -> httpx.Response:
    return client.request(
        method,
        path,
        headers=dict(headers or {}),
        json=json,
        params=dict(params) if params is not None else None,
        auth=auth,
    )


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    auth: httpx.Auth | None = None,
    json: dict[str, Any] | None = None,
    params: Mapping[str, str] | None = None,
    failure: type[Exception] = SmokeFailure,
    redact: Redactor | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    response = request(
        client,
        method,
        path,
        headers=headers,
        auth=auth,
        json=json,
        params=params,
    )
    assert_status(response, expected_status, path, failure=failure, redact=redact)
    return response_json(response, path, failure=failure, redact=redact)


# --------------------------------------------------------------------------
# Build metadata
# --------------------------------------------------------------------------


def sha_prefix_match(actual: str, expected: str) -> bool:
    """True when either commit sha is a prefix of the other.

    Deployments report a short sha in some fields and a full one in others, so
    a plain equality check would spuriously fail.
    """
    actual = actual.strip().lower()
    expected = expected.strip().lower()
    return actual.startswith(expected) or expected.startswith(actual)


def assert_build_sha(
    actual: Any,
    expected: str,
    label: str,
    *,
    failure: type[Exception] = SmokeFailure,
) -> None:
    if not isinstance(actual, str) or not actual:
        raise failure(f"{label}: expected deployed commit {expected[:12]}, got none")
    if not sha_prefix_match(actual, expected):
        raise failure(
            f"{label}: expected deployed commit {expected[:12]}, got {actual[:12]}"
        )


def assert_build_info(
    build: Any,
    expected_version: str,
    *,
    label: str = "healthz build",
    failure: type[Exception] = SmokeFailure,
) -> dict[str, Any]:
    if not isinstance(build, dict):
        raise failure(f"{label}: expected build metadata object")
    assert_equal(build.get("version"), expected_version, f"{label} version", failure=failure)
    for name in ("commit_sha", "commit_sha_short", "branch", "deployment_id"):
        value = build.get(name)
        if value is not None and not isinstance(value, str):
            raise failure(f"{label} {name}: expected string or null")
    return build


# --------------------------------------------------------------------------
# URLs and secrets
# --------------------------------------------------------------------------


def allowed_hosts_from_env(
    env_name: str,
    defaults: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Hosts a smoke script may send Basic Auth secrets to.

    Overridable with ``env_name`` (comma separated, a leading dot matches
    subdomains) -- an env var, so it cannot be set from a dispatch input.
    """
    source = environ if environ is not None else os.environ
    raw = (source.get(env_name) or "").strip()
    if raw:
        return tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    return tuple(defaults)


def host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    if not host:
        return False
    for entry in allowed_hosts:
        entry = entry.strip().lower()
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def normalize_base_url(
    value: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
    failure: type[Exception] = SmokeFailure,
    scheme_error: str = "Expected an HTTP(S) URL such as https://demo.example.com",
    host_error: Callable[[str, Sequence[str]], str] | None = None,
) -> str:
    """Validate and canonicalize a smoke-test base URL.

    ``allowed_hosts=None`` performs the scheme/netloc check only. Pass a host
    allowlist for any URL that will carry credentials -- see the module
    docstring.
    """
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise failure(scheme_error)
    if allowed_hosts is None:
        return base_url
    hosts = tuple(allowed_hosts)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host_allowed(host, hosts):
        if host_error is not None:
            raise failure(host_error(host, hosts))
        raise failure(
            f"Base URL host {host!r} is not allowed. This smoke script attaches "
            "Basic Auth credentials to every authenticated request, so it only "
            f"runs against {', '.join(hosts)}."
        )
    return base_url


def normalize_optional_origin(
    value: str | None,
    *,
    failure: type[Exception] = SmokeFailure,
    error: str = "CORS origins must be HTTP(S) origins such as https://demo.example.com",
) -> str | None:
    if not value or not value.strip():
        return None
    origin = value.strip().rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise failure(error)
    return f"{parsed.scheme}://{parsed.netloc}"


def origin_from_url(value: str) -> str:
    parsed = urlparse(value)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def secret_values(
    *extra_values: str | None,
    environ: Mapping[str, str] | None = None,
) -> set[str]:
    """Every value worth scrubbing out of printed output.

    The explicitly passed values plus anything in the environment whose name
    looks like a credential -- a smoke run inherits the same secrets the app
    does, and an echoed error body can contain any of them.
    """
    source = environ if environ is not None else os.environ
    values = {value for value in extra_values if value}
    for key, value in source.items():
        if value and any(token in key.lower() for token in SECRET_NAME_TOKENS):
            values.add(value)
    return {value for value in values if len(value) >= MIN_SECRET_LENGTH}


def redact_secrets(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        redacted = redacted.replace(secret, "[redacted]")
    return redacted

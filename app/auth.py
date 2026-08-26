from __future__ import annotations

import base64
import os
import re
import secrets
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


DEFAULT_TENANT_ID = "local-demo"
# Deployment signal: when truthy, the app refuses to start without Basic Auth.
# The container image sets it, so a shared/remote deploy that forgets the auth
# env vars fails the deploy instead of serving every state-changing endpoint
# openly. Local loopback demos opt out with REGENGINE_REQUIRE_AUTH=0.
REQUIRE_AUTH_ENV = "REGENGINE_REQUIRE_AUTH"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
TENANT_HEADER = "X-RegEngine-Tenant"
_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class BasicAuthConfig:
    username: str | None
    password: str | None
    default_tenant: str = DEFAULT_TENANT_ID

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    auth_enabled: bool = False
    username: str | None = None

    @property
    def uses_default_storage(self) -> bool:
        return self.tenant_id == DEFAULT_TENANT_ID and not self.auth_enabled


def basic_auth_config_from_env() -> BasicAuthConfig:
    return BasicAuthConfig(
        username=os.getenv("REGENGINE_BASIC_AUTH_USERNAME"),
        password=os.getenv("REGENGINE_BASIC_AUTH_PASSWORD"),
        default_tenant=normalize_tenant_id(
            os.getenv("REGENGINE_DEFAULT_TENANT", DEFAULT_TENANT_ID)
        ),
    )


def auth_required_from_env() -> bool:
    """True when this deployment profile must enforce Basic Auth."""
    return os.getenv(REQUIRE_AUTH_ENV, "").strip().lower() in _TRUTHY


def enforce_auth_requirement(config: BasicAuthConfig | None = None) -> None:
    """Fail closed when a shared/remote profile is missing Basic Auth.

    Called from the startup lifespan so a deployment that sets
    REGENGINE_REQUIRE_AUTH (the container image does) but forgets
    REGENGINE_BASIC_AUTH_USERNAME/PASSWORD refuses to boot rather than
    serving every state-changing endpoint unauthenticated.
    """
    config = config or basic_auth_config_from_env()
    if auth_required_from_env() and not config.enabled:
        raise RuntimeError(
            f"{REQUIRE_AUTH_ENV} is set but REGENGINE_BASIC_AUTH_USERNAME/"
            "REGENGINE_BASIC_AUTH_PASSWORD are not both configured. Refusing to "
            f"start a shared/remote deployment without authentication. Set the "
            f"credentials, or set {REQUIRE_AUTH_ENV}=0 for a local loopback demo."
        )


def tenant_context_from_request(request: Request) -> TenantContext | JSONResponse:
    config = basic_auth_config_from_env()
    username = None
    if config.enabled:
        credentials = _parse_basic_authorization(request.headers.get("authorization"))
        if credentials is None:
            return _unauthorized_response()

        supplied_username, supplied_password = credentials
        # Both comparisons must always run: `and` short-circuits, which would
        # skip the password comparison on a username miss and leak username
        # validity through response timing. Evaluate first, combine with `&`.
        username_ok = secrets.compare_digest(supplied_username, config.username or "")
        password_ok = secrets.compare_digest(supplied_password, config.password or "")
        if not (username_ok & password_ok):
            return _unauthorized_response()
        username = supplied_username

    requested_tenant = request.headers.get(TENANT_HEADER) or username or config.default_tenant
    try:
        tenant_id = normalize_tenant_id(requested_tenant)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return TenantContext(
        tenant_id=tenant_id,
        auth_enabled=config.enabled,
        username=username,
    )


def normalize_tenant_id(value: str) -> str:
    tenant_id = value.strip()
    if not _TENANT_PATTERN.fullmatch(tenant_id):
        raise ValueError(
            "Tenant id must be 1-64 characters and contain only letters, numbers, dots, underscores, or hyphens"
        )
    return tenant_id


def _parse_basic_authorization(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
        headers={"WWW-Authenticate": 'Basic realm="RegEngine Inflow Lab"'},
    )

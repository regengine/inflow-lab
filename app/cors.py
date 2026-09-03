from __future__ import annotations

import logging
import os
from urllib.parse import urlparse


DEFAULT_CORS_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000")

logger = logging.getLogger("inflow_lab.cors")


def cors_origins_from_env() -> list[str]:
    """Strict parse of REGENGINE_CORS_ORIGINS: raises ValueError on the first
    malformed entry. Kept for direct/test callers that want that contract.
    create_app() does not call this directly -- it calls
    resolve_cors_origins() below, which never raises (#178): a single typo in
    this hand-typed, operator-facing variable must not take the whole
    process down before it binds a port.
    """
    return _with_platform_origin(_parsed_env_origins(strict=True))


def resolve_cors_origins() -> list[str]:
    """CORS origins for the running app: never raises.

    Runs the same per-entry validation as cors_origins_from_env(), but a
    malformed entry is skipped and logged instead of raised, so the other
    origins an operator configured still take effect. This is the same
    trade-off _platform_origin() below already makes for RAILWAY_PUBLIC_DOMAIN
    -- degrade, don't crash, while the ASGI app is being constructed.
    """
    return _with_platform_origin(_parsed_env_origins(strict=False))


def _parsed_env_origins(*, strict: bool) -> list[str]:
    raw_origins = os.getenv("REGENGINE_CORS_ORIGINS")
    if not raw_origins or not raw_origins.strip():
        return list(DEFAULT_CORS_ORIGINS)

    origins: list[str] = []
    for raw_origin in raw_origins.split(","):
        try:
            origin = _normalize_cors_origin(raw_origin)
        except ValueError:
            if strict:
                raise
            logger.warning(
                "Ignoring malformed REGENGINE_CORS_ORIGINS entry %r; keeping the other "
                "configured origins. Entries must be plain HTTP(S) origins such as "
                "https://demo.example.com.",
                raw_origin.strip(),
            )
            continue
        if origin and origin not in origins:
            origins.append(origin)
    return origins or list(DEFAULT_CORS_ORIGINS)


def _with_platform_origin(origins: list[str]) -> list[str]:
    # The service always trusts its own platform-issued domain, IN ADDITION to
    # whatever is configured. A union, not a fallback, on purpose: in the
    # August 2026 cutover REGENGINE_CORS_ORIGINS was set — to the previous
    # service's URL, via a Railway reference variable — so a fallback would
    # have changed nothing, and the service rejected every browser request
    # from its own domain for three days (see DEPLOYMENT_PROFILES.md,
    # "Moving the demo to a new service"). Trusting RAILWAY_PUBLIC_DOMAIN is
    # safe because whoever controls that variable controls the deployment
    # itself; it widens nothing beyond the service's own canonical origin.
    platform_origin = _platform_origin()
    if platform_origin and platform_origin not in origins:
        origins.append(platform_origin)
    return origins


def _platform_origin() -> str | None:
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if not domain:
        return None
    if "://" not in domain:
        domain = f"https://{domain}"
    try:
        return _normalize_cors_origin(domain)
    except ValueError:
        # A malformed platform value must degrade to "no extra origin", never
        # crash startup — this runs while the ASGI app is being constructed.
        return None


def _normalize_cors_origin(raw_origin: str) -> str | None:
    origin = raw_origin.strip().rstrip("/")
    if not origin:
        return None
    if origin == "*":
        raise ValueError("REGENGINE_CORS_ORIGINS cannot contain '*' while credentialed requests are enabled")

    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "REGENGINE_CORS_ORIGINS entries must be comma-separated HTTP(S) origins such as "
            "https://demo.example.com"
        )
    return f"{parsed.scheme}://{parsed.netloc}"

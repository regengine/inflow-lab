from __future__ import annotations

import logging
import os
from functools import lru_cache
from urllib.parse import urlparse

<<<<<<< HEAD
=======

logger = logging.getLogger(__name__)

>>>>>>> origin/main
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


@lru_cache(maxsize=16)
def _resolved_origins_for_env(_raw_origins: str | None, _platform_domain: str | None) -> tuple[str, ...]:
    # The two arguments are the cache KEY only -- resolve_cors_origins() reads
    # both back out of os.environ itself. Passing them in is precisely what
    # makes the cache invalidate the moment either variable changes, so a test
    # (or an operator editing the deployment) never sees a stale answer.
    return tuple(resolve_cors_origins())


def resolve_cors_origins_cached() -> list[str]:
    """resolve_cors_origins(), memoized on the environment it derives from.

    For the request path (#200). auth_middleware consults the trusted-origin
    list on every authenticated state-changing request; parsing the variable
    from scratch each time is pure waste, since it only changes when the
    process is reconfigured.
    """
    return list(
        _resolved_origins_for_env(
            os.getenv("REGENGINE_CORS_ORIGINS"), os.getenv("RAILWAY_PUBLIC_DOMAIN")
        )
    )


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


def cors_origins_for_app() -> list[str]:
    """CORS allowlist for app construction — never raises.

    ``cors_origins_from_env`` deliberately raises on a malformed entry so
    direct callers (and tests) see the error. ``create_app`` runs at import
    time, though, so letting that raise escape turns one mistyped entry in the
    hand-edited ``REGENGINE_CORS_ORIGINS`` list into a full outage: the process
    never binds a port, and every tenant and endpoint goes down rather than
    just cross-origin browser calls. Degrade to the built-in defaults (plus the
    platform origin) and log a loud warning naming the rejected value, the same
    way ``_platform_origin`` already degrades for RAILWAY_PUBLIC_DOMAIN.
    """
    try:
        return cors_origins_from_env()
    except ValueError as exc:
        logger.warning(
            "Ignoring malformed REGENGINE_CORS_ORIGINS=%r (%s); falling back to the default "
            "CORS allowlist. Fix the value to restore the configured origins.",
            os.getenv("REGENGINE_CORS_ORIGINS"),
            exc,
        )

    origins = list(DEFAULT_CORS_ORIGINS)
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

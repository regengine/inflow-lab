from __future__ import annotations

import os
from urllib.parse import urlparse

DEFAULT_CORS_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000")


def cors_origins_from_env() -> list[str]:
    raw_origins = os.getenv("REGENGINE_CORS_ORIGINS")
    if not raw_origins or not raw_origins.strip():
        origins = list(DEFAULT_CORS_ORIGINS)
    else:
        origins = []
        for raw_origin in raw_origins.split(","):
            origin = _normalize_cors_origin(raw_origin)
            if origin and origin not in origins:
                origins.append(origin)
        origins = origins or list(DEFAULT_CORS_ORIGINS)

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

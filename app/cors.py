from __future__ import annotations

import os
from urllib.parse import urlparse


DEFAULT_CORS_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000")


def cors_origins_from_env() -> list[str]:
    raw_origins = os.getenv("REGENGINE_CORS_ORIGINS")
    if not raw_origins or not raw_origins.strip():
        return list(DEFAULT_CORS_ORIGINS)

    origins: list[str] = []
    for raw_origin in raw_origins.split(","):
        origin = _normalize_cors_origin(raw_origin)
        if origin and origin not in origins:
            origins.append(origin)
    return origins or list(DEFAULT_CORS_ORIGINS)


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

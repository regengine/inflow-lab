"""Protections ported from the parallel audit branch (PR #198) into this stack.

Each test here pins a behaviour that existed on that branch and was absent
here, so that closing #198 does not quietly drop it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.mock_service import MAX_INGEST_BODY_BYTES
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import (
    ALLOW_CLEARTEXT_DELIVERY_ENV,
    ALLOWED_DELIVERY_HOSTS_ENV,
    EgressBlockedError,
    resolve_egress_endpoint,
)
from pydantic import HttpUrl


# --- ingest body ceiling ---------------------------------------------------


def test_an_oversized_declared_body_is_refused_without_being_read() -> None:
    """Content-Length above the ceiling is a 413 before any bytes are read."""
    with TestClient(app) as client:
        response = client.post(
            "/api/mock/regengine/ingest",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(MAX_INGEST_BODY_BYTES + 1),
            },
        )
    assert response.status_code == 413
    assert "ingest ceiling" in response.json()["detail"]


def test_an_ordinary_body_still_passes_the_ceiling() -> None:
    with TestClient(app) as client:
        response = client.post("/api/mock/regengine/ingest", json={"events": []})
    # Rejected on the empty batch, not on size -- the ceiling did not fire.
    assert response.status_code == 422


# --- cleartext delivery ----------------------------------------------------


def test_cleartext_delivery_to_a_public_host_is_refused() -> None:
    with pytest.raises(EgressBlockedError, match="cleartext"):
        resolve_egress_endpoint(HttpUrl("http://partner.example.net/webhooks/ingest"))


def test_cleartext_opt_in_allows_a_public_http_endpoint(monkeypatch: Any) -> None:
    monkeypatch.setenv(ALLOW_CLEARTEXT_DELIVERY_ENV, "1")
    resolve_egress_endpoint(HttpUrl("http://partner.example.net/webhooks/ingest"))


def test_https_is_unaffected() -> None:
    resolve_egress_endpoint(HttpUrl("https://www.regengine.co/api/v1/webhooks/ingest"))


# --- operator egress allowlist ---------------------------------------------


def test_allowlist_refuses_a_host_outside_the_list(monkeypatch: Any) -> None:
    monkeypatch.setenv(ALLOWED_DELIVERY_HOSTS_ENV, ".regengine.co")
    with pytest.raises(EgressBlockedError, match=ALLOWED_DELIVERY_HOSTS_ENV):
        resolve_egress_endpoint(HttpUrl("https://attacker.example/ingest"))


def test_allowlist_leading_dot_matches_subdomains(monkeypatch: Any) -> None:
    monkeypatch.setenv(ALLOWED_DELIVERY_HOSTS_ENV, ".regengine.co")
    resolve_egress_endpoint(HttpUrl("https://www.regengine.co/api/v1/webhooks/ingest"))


def test_allowlist_is_not_widened_by_the_private_host_opt_out(monkeypatch: Any) -> None:
    """A local-dev flag must not defeat an operator's explicit egress pin."""
    monkeypatch.setenv(ALLOWED_DELIVERY_HOSTS_ENV, ".regengine.co")
    monkeypatch.setenv("REGENGINE_ALLOW_PRIVATE_ENDPOINTS", "1")
    with pytest.raises(EgressBlockedError):
        resolve_egress_endpoint(HttpUrl("https://attacker.example/ingest"))


# --- idempotency replay detection ------------------------------------------


def test_a_fresh_key_is_never_a_replay() -> None:
    """Nothing can have been cached under a key that was just minted."""
    from app.controller import _is_idempotency_replay

    payload = IngestPayload(events=[])
    assert not _is_idempotency_replay({"events": [1, 2, 3]}, payload, reused_key=False)


def test_a_summary_only_response_is_not_a_replay() -> None:
    """A receiver answering {"accepted": n} has produced no per-event verdicts."""
    from app.controller import _is_idempotency_replay

    payload = IngestPayload(events=[])
    assert not _is_idempotency_replay(
        {"accepted": 3, "rejected": 0}, payload, reused_key=True
    )
    assert not _is_idempotency_replay({"events": []}, payload, reused_key=True)


def test_a_reused_key_with_a_mismatched_count_is_still_a_replay() -> None:
    from app.controller import _is_idempotency_replay

    payload = IngestPayload(events=[])
    assert _is_idempotency_replay({"events": [1, 2]}, payload, reused_key=True)


# --- spreadsheet formula injection in the FDA export (#65) ------------------


@pytest.mark.parametrize("payload", ["=cmd|'/c calc'!A1", "+1+1", "-1+1", "@SUM(A1)"])
def test_formula_leaders_are_neutralised_in_the_export(payload: str) -> None:
    """A lot code reaches this export from CSV import and from the API.

    Excel, LibreOffice and Sheets evaluate a cell beginning with one of these,
    so an unescaped value executes when a reviewer opens the regulatory export.
    """
    from app.fda_export import _sanitize_cell

    assert _sanitize_cell(payload) == "'" + payload


def test_ordinary_values_are_untouched() -> None:
    from app.fda_export import _sanitize_cell

    assert _sanitize_cell("LOT-2026-001") == "LOT-2026-001"
    assert _sanitize_cell(42) == 42

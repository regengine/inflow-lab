"""The ingest route's authentication gate: what runs before the body is held.

#209/#210 made HMAC verification the outermost gate on
POST /api/mock/regengine/ingest -- the raw bytes are verified before they
are decoded or model-validated, so an unsigned caller gets a flat 401
instead of a schema-enumerating 422. tests/test_mock_hmac.py covers that
ordering.

This module covers the step after it. Three of the four ways a signature
is rejected -- no header at all, a scheme that is not sha256, and a digest
that is not hexadecimal -- are decided from the header alone and never
consult the body. Reading the body first was therefore work an
unauthenticated caller could always make the server do for nothing:
Starlette's ``Request.body()`` accumulates the chunk list AND the joined
result, so a 50 MB unsigned body cost ~100 MB of resident memory before
the 401 it was always going to get.

The invariant pinned here is the ordering *inside* the gate:

- a rejection decided by the header alone must never accumulate the body,
- but it must still DRAIN it, so the client can finish its upload and
  actually read the 401 rather than a connection reset,
- and a signature that could plausibly verify must still read the body,
  because that is the only way to compare against it.

That last one matters as much as the first two: "never read the body" is
not the goal and would silently disable verification. The goal is to read
it only for a caller who has presented something worth checking.

``Request.body`` is spied on rather than measuring memory, because the
accumulation is exactly what the spy observes and a byte count is
deterministic where an RSS reading is not.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_lib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
import starlette.requests
from fastapi.testclient import TestClient

from app.main import app, controller
from app.mock_service import MockRegEngineHTTPError, parse_signature_digest
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig

HMAC_ENV_VAR = "REGENGINE_WEBHOOK_HMAC_SECRET"
INGEST_URL = "/api/mock/regengine/ingest"

client = TestClient(app)


def setup_function() -> None:
    # Same isolation pattern as tests/test_mock_hmac.py and
    # tests/test_mock_parity.py: reset the shared app-global controller so
    # chain-hash and idempotency state cannot leak in from a module that
    # ran earlier in the same process. This suite has had real
    # order-dependence bugs, so every module resets rather than assuming.
    asyncio.run(controller.reset(SimulationConfig()))


def _valid_event(lot: str = "TLC-GATE-000001") -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code=lot,
        product_description="Romaine Lettuce",
        quantity=100,
        unit_of_measure="cases",
        location_name="Valley Fresh Farms",
        timestamp=datetime(2026, 2, 5, 8, 0, tzinfo=UTC),
        kdes={
            "harvest_date": "2026-02-05",
            "reference_document": "Harvest Log HAR-0001",
        },
    )


def _canonical_body(lot: str = "TLC-GATE-000001") -> bytes:
    """The exact bytes LiveRegEngineClient.ingest() would put on the wire.

    Same separators/sort_keys as app/regengine_client.py, so what is signed
    here is what a real client signs.
    """
    payload = IngestPayload(source="gate-test", events=[_valid_event(lot=lot)])
    return json.dumps(
        payload.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sign(secret: str, body: bytes) -> str:
    return f"sha256={hmac_lib.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()}"


class _BodySpy:
    """Records whether the route accumulated the body, and how much it streamed.

    ``streamed_bytes`` counts only plain ``Request`` objects -- the one
    FastAPI hands the endpoint. The app also runs a ``BaseHTTPMiddleware``
    (``auth_and_tenant_middleware``), whose ``_CachedRequest`` subclass
    relays the same chunks one layer up; counting those too would double
    every figure and measure Starlette's plumbing rather than this route's
    behaviour.
    """

    def __init__(self) -> None:
        self.body_calls = 0
        self.streamed_bytes = 0

    def install(self, monkeypatch: Any) -> None:
        real_body = starlette.requests.Request.body
        real_stream = starlette.requests.Request.stream
        spy = self

        async def body(self: starlette.requests.Request) -> bytes:
            if type(self) is starlette.requests.Request:
                spy.body_calls += 1
            return await real_body(self)

        async def stream(self: starlette.requests.Request):  # type: ignore[no-untyped-def]
            count = type(self) is starlette.requests.Request
            async for chunk in real_stream(self):
                if count:
                    spy.streamed_bytes += len(chunk)
                yield chunk

        monkeypatch.setattr(starlette.requests.Request, "body", body)
        monkeypatch.setattr(starlette.requests.Request, "stream", stream)


# A body big enough that holding it would be the whole cost of the request,
# but small enough to keep the suite fast. The defect is about ratio, not
# absolute size: whatever this is, an unsigned caller should pay none of it.
_BIG_BODY = json.dumps(
    {"source": "gate-test", "events": [{"junk": "x" * 512, "n": i} for i in range(4000)]}
).encode("utf-8")


# ---------------------------------------------------------------------------
# Rejections decided from the header alone must not accumulate the body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("signature", "expected_detail"),
    [
        (None, "Missing X-Webhook-Signature header"),
        ("md5=" + "a" * 32, "Unsupported X-Webhook-Signature scheme (expected 'sha256=<hex>')"),
        ("sha256=" + "z" * 64, "Invalid X-Webhook-Signature"),
        ("sha256=", "Unsupported X-Webhook-Signature scheme (expected 'sha256=<hex>')"),
    ],
)
def test_header_only_rejections_never_accumulate_the_body(
    monkeypatch: Any, signature: str | None, expected_detail: str
) -> None:
    """No header, wrong scheme, or a non-hex digest: all decided before the read.

    Each of these is a verdict the bytes cannot change -- there is no body
    that makes an md5 scheme or a "zzz..." digest valid -- so the route
    must reach it without ever calling Request.body(), which is what would
    hold the payload in memory (#209).
    """
    monkeypatch.setenv(HMAC_ENV_VAR, "gate-shared-secret")
    spy = _BodySpy()
    spy.install(monkeypatch)

    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature

    response = client.post(INGEST_URL, content=_BIG_BODY, headers=headers)

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == expected_detail
    assert spy.body_calls == 0, (
        "the body was accumulated for a rejection the header alone already decided"
    )


@pytest.mark.parametrize("signature", [None, "md5=" + "a" * 32, "sha256=" + "z" * 64])
def test_header_only_rejections_still_drain_the_body(monkeypatch: Any, signature: str | None) -> None:
    """Not reading is not the same as not draining.

    Answering while the client is still writing makes the server close the
    connection under it, and the caller sees a reset instead of the 401
    naming what was wrong with their header -- which is the one thing this
    simulator exists to tell them. So the bytes are streamed and discarded
    rather than skipped. TestClient cannot observe a reset (it is
    in-memory), so what is pinned here is the drain itself: every byte the
    client sent was consumed.
    """
    monkeypatch.setenv(HMAC_ENV_VAR, "gate-shared-secret")
    spy = _BodySpy()
    spy.install(monkeypatch)

    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature

    response = client.post(INGEST_URL, content=_BIG_BODY, headers=headers)

    assert response.status_code == 401, response.text
    assert spy.streamed_bytes == len(_BIG_BODY), (
        f"drained {spy.streamed_bytes} of {len(_BIG_BODY)} bytes; a body left unread "
        "makes the client see a connection reset instead of the 401"
    )


# ---------------------------------------------------------------------------
# ...but a signature worth checking must still be checked against the body
# ---------------------------------------------------------------------------


def test_a_well_formed_signature_still_reads_the_body_to_compare_it(monkeypatch: Any) -> None:
    """The guard against 'optimizing' the read away entirely.

    A syntactically valid digest can only be judged against the bytes it
    claims to cover. If this ever stops calling Request.body(), signature
    verification has been disabled rather than reordered -- so the wrong
    digest below must cost a real HMAC over a real body.
    """
    monkeypatch.setenv(HMAC_ENV_VAR, "gate-shared-secret")
    spy = _BodySpy()
    spy.install(monkeypatch)

    response = client.post(
        INGEST_URL,
        content=_canonical_body(),
        headers={"Content-Type": "application/json", "X-Webhook-Signature": "sha256=" + "0" * 64},
    )

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid X-Webhook-Signature"
    assert spy.body_calls == 1, "a plausible digest must be compared against the real bytes"


def test_correctly_signed_request_is_unchanged_by_the_reordering(monkeypatch: Any) -> None:
    """The signed path must be byte-for-byte what it was: 200, event accepted."""
    secret = "gate-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = _canonical_body()

    response = client.post(
        INGEST_URL,
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": _sign(secret, raw_body)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 0
    assert body["events"][0]["status"] == "accepted"


def test_unsigned_request_is_untouched_when_no_secret_is_configured(monkeypatch: Any) -> None:
    """The pre-signing migration ramp still works.

    With no secret set there is nothing to check, so the header-shape gate
    must not run at all -- an unsigned request still ingests normally.
    """
    monkeypatch.delenv(HMAC_ENV_VAR, raising=False)
    spy = _BodySpy()
    spy.install(monkeypatch)

    response = client.post(
        INGEST_URL,
        content=_canonical_body(lot="TLC-GATE-000002"),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    assert spy.body_calls == 1, "the body is still read normally when signing is not enforced"


# ---------------------------------------------------------------------------
# parse_signature_digest: the header-only half, on its own
# ---------------------------------------------------------------------------


def test_parse_signature_digest_returns_the_hex_digest() -> None:
    assert parse_signature_digest("sha256=" + "AbCdEf" + "0" * 58) == "AbCdEf" + "0" * 58


@pytest.mark.parametrize(
    ("header", "status"),
    [
        (None, 401),
        ("", 401),
        ("sha256=", 401),
        ("sha256=café" + "0" * 60, 401),
        ("sha1=" + "a" * 40, 401),
        ("nonsense", 401),
        ("=" + "a" * 64, 401),
    ],
)
def test_parse_signature_digest_rejects_every_unusable_header(header: str | None, status: int) -> None:
    """Including the non-ASCII case that used to reach hmac.compare_digest.

    compare_digest raises TypeError -- not False -- on a str carrying a
    non-ASCII character, which escaped as an unhandled 500 with a traceback
    to an unauthenticated caller (#209). Rejecting non-hex here is what
    keeps that byte from ever reaching the comparison, and it now happens
    before the body is read as well.
    """
    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        parse_signature_digest(header)

    assert exc_info.value.status_code == status

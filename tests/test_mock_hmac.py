"""HMAC signature verification and header threading for the mock ingest path.

Covers two gaps identified in a repo-wide mock-fidelity audit:

- #113: MockRegEngineService.ingest() and POST /api/mock/regengine/ingest
  never verified X-Webhook-Signature, so a signing bug in
  LiveRegEngineClient (app/regengine_client.py) only ever surfaced against
  a live/staging RegEngine deployment. The mock now verifies HMAC-SHA256
  over the exact raw request body bytes -- the same bytes
  LiveRegEngineClient signs -- whenever REGENGINE_WEBHOOK_HMAC_SECRET is
  configured, gated the same way live RegEngine's own verifier is (see
  docs/HMAC_STAGING_VALIDATION.md): unset secret means no enforcement.
- #116: the HTTP route ignored the Idempotency-Key header and mock_friction
  entirely, unlike SimulationController's internal delivery path
  (mock_service.ingest(payload, idempotency_key=..., friction=...) in
  app/controller.py). The route now reads Idempotency-Key and
  X-Mock-Friction headers and threads them through, so both paths agree.

Tests that only need signature/idempotency/friction semantics in isolation
construct their own MockRegEngineService directly. Tests for the HTTP route
specifically (raw-header handling, status codes, route/service agreement)
go through the real FastAPI app via TestClient, mirroring
tests/test_mock_parity.py's own split.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_lib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.mock_service import MockRegEngineHTTPError, MockRegEngineService
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig

HMAC_ENV_VAR = "REGENGINE_WEBHOOK_HMAC_SECRET"
INGEST_URL = "/api/mock/regengine/ingest"

client = TestClient(app)


def setup_function() -> None:
    # Same isolation pattern as tests/test_mock_parity.py and
    # tests/test_integration_settings.py: reset the shared app-global
    # controller/store/mock_service before each test so idempotency-cache
    # and chain-hash state never leak between tests in this module (or
    # from another test module that ran earlier in the same `pytest -q`
    # process).
    asyncio.run(controller.reset(SimulationConfig()))


def _valid_event(lot: str = "TLC-HMAC-000001") -> RegEngineEvent:
    """A minimal HARVESTING event that clears every validate_event_like_regengine check.

    Mirrors tests/test_mock_parity.py's own helper: HARVESTING has the
    fewest required KDEs (harvest_date, reference_document).
    """
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


def _canonical_body(source: str = "hmac-test", lot: str = "TLC-HMAC-000001") -> bytes:
    """Serialize a payload exactly the way LiveRegEngineClient.ingest() does.

    app/regengine_client.py signs
    json.dumps(payload.model_dump(mode="json"), separators=(",", ":"),
    sort_keys=True).encode("utf-8") and sends those exact bytes via httpx
    content=. Building the same canonical bytes here -- rather than
    e.g. relying on TestClient's own json= serialization, which uses
    different separators and key order -- is the point: a mismatch here
    would just be testing this file's own serializer instead of the
    wire contract the client and mock actually have to agree on.
    """
    payload = IngestPayload(source=source, events=[_valid_event(lot=lot)])
    return json.dumps(
        payload.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sign(secret: str, body: bytes) -> str:
    digest = hmac_lib.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload_dict(source: str = "hmac-test", lot: str = "TLC-HMAC-000001") -> dict[str, Any]:
    return {"source": source, "events": [_valid_event(lot=lot).model_dump(mode="json")]}


# ---------------------------------------------------------------------------
# #113: MockRegEngineService.ingest() signature verification
# ---------------------------------------------------------------------------


def test_service_accepts_correctly_signed_body(monkeypatch: Any) -> None:
    secret = "unit-test-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    service = MockRegEngineService()

    response = service.ingest(
        payload, raw_body=raw_body, signature_header=_sign(secret, raw_body)
    )

    assert response.accepted == 1


def test_service_rejects_body_modified_after_signing(monkeypatch: Any) -> None:
    # Simulates the exact "Body-Bytes Drift" bug docs/HMAC_STAGING_VALIDATION.md
    # warns about: the signature was computed over one set of bytes, but a
    # different set of bytes is what actually arrived. Verifying against
    # re-derived canonical bytes (instead of the real raw_body) would miss
    # this entirely -- which is the whole reason #113 exists.
    secret = "unit-test-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    original_body = _canonical_body()
    signature = _sign(secret, original_body)
    tampered_body = original_body.replace(b'"quantity":100.0', b'"quantity":999.0')
    assert tampered_body != original_body
    payload = IngestPayload.model_validate_json(tampered_body)
    service = MockRegEngineService()

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload, raw_body=tampered_body, signature_header=signature)

    assert exc_info.value.status_code == 401


def test_service_rejects_wrong_signature_over_correct_body(monkeypatch: Any) -> None:
    monkeypatch.setenv(HMAC_ENV_VAR, "correct-secret")
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    wrong_signature = _sign("a-completely-different-secret", raw_body)
    service = MockRegEngineService()

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload, raw_body=raw_body, signature_header=wrong_signature)

    assert exc_info.value.status_code == 401


def test_service_rejects_missing_signature_header_when_secret_configured(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(HMAC_ENV_VAR, "unit-test-shared-secret")
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    service = MockRegEngineService()

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload, raw_body=raw_body, signature_header=None)

    assert exc_info.value.status_code == 401
    assert "signature" in exc_info.value.detail.lower()


def test_service_rejects_unsupported_signature_scheme(monkeypatch: Any) -> None:
    monkeypatch.setenv(HMAC_ENV_VAR, "unit-test-shared-secret")
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    service = MockRegEngineService()

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload, raw_body=raw_body, signature_header="md5=deadbeef")

    assert exc_info.value.status_code == 401


def test_service_skips_verification_when_secret_unset(monkeypatch: Any) -> None:
    # Behavior with the secret unset must be unchanged: no header, no
    # verification, request still succeeds -- matching both RegEngine's
    # own verifier no-op and LiveRegEngineClient's skip-signing behavior.
    monkeypatch.delenv(HMAC_ENV_VAR, raising=False)
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    service = MockRegEngineService()

    response = service.ingest(payload, raw_body=raw_body, signature_header=None)

    assert response.accepted == 1


def test_service_skips_verification_when_secret_is_blank(monkeypatch: Any) -> None:
    # A whitespace-only secret is treated as unset -- matches
    # LiveRegEngineClient's own _build_signature_header behavior (see
    # tests/test_regengine_client.py::test_live_client_treats_blank_secret_as_unset).
    monkeypatch.setenv(HMAC_ENV_VAR, "   ")
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    service = MockRegEngineService()

    response = service.ingest(payload, raw_body=raw_body, signature_header=None)

    assert response.accepted == 1


def test_service_internal_call_path_is_unaffected_by_secret(monkeypatch: Any) -> None:
    # SimulationController._deliver_payload's DestinationMode.MOCK branch
    # calls ingest() with no raw_body/signature_header at all -- it never
    # serializes or signs anything. Even with a secret configured, that
    # in-process call path must keep working exactly as before: there are
    # no wire bytes to check, so verification stays a no-op (body=None).
    monkeypatch.setenv(HMAC_ENV_VAR, "unit-test-shared-secret")
    payload = IngestPayload(source="internal-call", events=[_valid_event()])
    service = MockRegEngineService()

    response = service.ingest(payload, idempotency_key="internal-key", friction=())

    assert response.accepted == 1


# ---------------------------------------------------------------------------
# #113: POST /api/mock/regengine/ingest signature verification
# ---------------------------------------------------------------------------


def test_route_accepts_correctly_signed_request(monkeypatch: Any) -> None:
    secret = "route-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = _canonical_body()

    response = client.post(
        INGEST_URL,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": _sign(secret, raw_body),
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_route_rejects_tampered_body(monkeypatch: Any) -> None:
    secret = "route-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = _canonical_body()
    signature = _sign(secret, raw_body)
    tampered_body = raw_body.replace(b'"quantity":100.0', b'"quantity":999.0')
    assert tampered_body != raw_body

    response = client.post(
        INGEST_URL,
        content=tampered_body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
    )

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()


def test_route_rejects_wrong_signature(monkeypatch: Any) -> None:
    secret = "route-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = _canonical_body()

    response = client.post(
        INGEST_URL,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": _sign("wrong-secret", raw_body),
        },
    )

    assert response.status_code == 401


def test_route_rejects_missing_signature_header(monkeypatch: Any) -> None:
    monkeypatch.setenv(HMAC_ENV_VAR, "route-shared-secret")
    raw_body = _canonical_body()

    response = client.post(
        INGEST_URL, content=raw_body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()


def test_route_accepts_unsigned_request_when_secret_unset(monkeypatch: Any) -> None:
    # Behavior with the secret unset is unchanged through the HTTP route
    # too -- same acceptance criterion as the service-level test above,
    # exercised through the real request/response cycle.
    monkeypatch.delenv(HMAC_ENV_VAR, raising=False)

    response = client.post(INGEST_URL, json=_payload_dict())

    assert response.status_code == 200
    assert response.json()["accepted"] == 1


# ---------------------------------------------------------------------------
# #116: Idempotency-Key threaded through the HTTP route
# ---------------------------------------------------------------------------


def test_route_replays_cached_response_for_repeated_idempotency_key() -> None:
    payload = _payload_dict(lot="TLC-HMAC-IDEMP")

    first = client.post(INGEST_URL, json=payload, headers={"Idempotency-Key": "route-key-1"})
    second = client.post(INGEST_URL, json=payload, headers={"Idempotency-Key": "route-key-1"})

    assert first.status_code == second.status_code == 200
    # Not just same shape -- the identical cached response, including the
    # event_id/chain_hash a fresh ingest would regenerate. This is what
    # distinguishes a true replay from a lucky coincidence.
    assert first.json() == second.json()


def test_route_distinguishes_different_idempotency_keys() -> None:
    payload = _payload_dict(lot="TLC-HMAC-IDEMP-DISTINCT")

    first = client.post(INGEST_URL, json=payload, headers={"Idempotency-Key": "route-key-a"})
    second = client.post(INGEST_URL, json=payload, headers={"Idempotency-Key": "route-key-b"})

    assert first.status_code == second.status_code == 200
    assert first.json()["events"][0]["event_id"] != second.json()["events"][0]["event_id"]


def test_route_without_idempotency_key_does_not_dedupe() -> None:
    # No header at all -- pre-#116 default behavior for a caller that
    # doesn't opt into replay dedup, unchanged by this fix.
    payload = _payload_dict(lot="TLC-HMAC-NO-IDEMP")

    first = client.post(INGEST_URL, json=payload)
    second = client.post(INGEST_URL, json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["events"][0]["event_id"] != second.json()["events"][0]["event_id"]


def test_route_and_direct_service_call_share_idempotency_cache() -> None:
    """The HTTP route and SimulationController._deliver_payload's internal
    call both delegate to active_controller.mock_service -- the same
    instance and the same idempotency cache. A key already used by one
    path must replay for the other: this is what "the route and the
    direct service call agree" (#116) means in practice, and it is the
    exact scenario the issue's Impact section describes (a caller
    replaying an Idempotency-Key against the route directly must not
    double-process, mutating chain_hash twice for one logical request).
    """
    payload_dict = _payload_dict(lot="TLC-HMAC-AGREE")

    direct = controller.mock_service.ingest(
        IngestPayload.model_validate(payload_dict), idempotency_key="shared-route-key"
    )
    response = client.post(
        INGEST_URL, json=payload_dict, headers={"Idempotency-Key": "shared-route-key"}
    )

    assert response.status_code == 200
    assert response.json() == direct.model_dump(mode="json")


# ---------------------------------------------------------------------------
# #116: mock_friction threaded through the HTTP route via X-Mock-Friction
# ---------------------------------------------------------------------------


def test_route_reads_x_mock_friction_header() -> None:
    response = client.post(
        INGEST_URL, json=_payload_dict(), headers={"X-Mock-Friction": "rate_limit"}
    )

    assert response.status_code == 429
    assert "Rate limit" in response.json()["detail"]


def test_route_without_x_mock_friction_header_behaves_as_before() -> None:
    response = client.post(INGEST_URL, json=_payload_dict())

    assert response.status_code == 200


def test_route_x_mock_friction_header_accepts_comma_separated_codes() -> None:
    # DeliveryConfig.mock_friction is a list; the header mirrors that by
    # accepting more than one code. ingest() raises on the first code in
    # the tuple that maps to a failure, so the first listed code wins.
    response = client.post(
        INGEST_URL,
        json=_payload_dict(),
        headers={"X-Mock-Friction": "invalid_key, rate_limit"},
    )

    assert response.status_code == 401
    assert "Invalid API key" in response.json()["detail"]


def test_route_friction_header_agrees_with_direct_service_friction() -> None:
    """The route's X-Mock-Friction header must produce the exact same
    rejection (status code and detail) as calling mock_service.ingest()
    directly with friction=("invalid_key",) -- the same guarantee
    test_route_and_direct_service_call_share_idempotency_cache checks for
    Idempotency-Key, applied to friction (#116)."""
    payload = IngestPayload.model_validate(_payload_dict(lot="TLC-HMAC-FRICTION-AGREE"))

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        controller.mock_service.ingest(payload, friction=("invalid_key",))

    response = client.post(
        INGEST_URL,
        json=_payload_dict(lot="TLC-HMAC-FRICTION-AGREE"),
        headers={"X-Mock-Friction": "invalid_key"},
    )

    assert response.status_code == exc_info.value.status_code == 401
    assert response.json()["detail"] == exc_info.value.detail


# ---------------------------------------------------------------------------
# #209: HMAC verification must be the OUTERMOST gate on the HTTP route
# ---------------------------------------------------------------------------


def test_route_401s_an_unsigned_body_without_ever_validating_its_schema(
    monkeypatch: Any,
) -> None:
    """An unsigned caller must get a flat 401, never a field-level 422.

    The route used to declare `payload: IngestPayload`, so FastAPI parsed
    and model-validated the entire batch before the handler -- and thus
    before the signature check -- ever ran. That handed an unauthenticated
    caller a schema oracle: send a deliberately malformed body and read the
    422 back to enumerate field names, types and constraints. It also meant
    an unsigned 50 MB body was fully decoded before being thrown away.

    The body below is malformed three ways at once (unknown envelope key,
    unknown per-event field, wrong type on quantity). Each of those is a
    422 that this suite pins elsewhere; with a secret configured and no
    signature, the caller must learn none of it.
    """
    monkeypatch.setenv(HMAC_ENV_VAR, "route-shared-secret")

    response = client.post(
        INGEST_URL,
        json={
            "sorce": "typo",
            "events": [{"cte_type": "harvesting", "product_desc": "x", "quantity": "not-a-number"}],
        },
    )

    assert response.status_code == 401, response.text
    body = response.text
    for leaked in ("sorce", "product_desc", "quantity", "extra_forbidden"):
        assert leaked not in body, f"unsigned caller was told about {leaked!r}"


def test_route_401s_an_unsigned_body_that_is_not_even_json(monkeypatch: Any) -> None:
    """The same ordering, one step earlier: bytes that cannot be decoded at
    all must still cost an unsigned caller a 401 rather than a JSON-decode
    422. Nothing may be parsed before the signature is checked."""
    monkeypatch.setenv(HMAC_ENV_VAR, "route-shared-secret")

    response = client.post(
        INGEST_URL,
        content=b"{not json at all",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401, response.text


def test_route_still_422s_a_malformed_body_once_the_signature_is_valid(
    monkeypatch: Any,
) -> None:
    """The flip side: moving the parse behind the signature check must not
    lose the parse. A correctly signed but malformed body still gets the
    same field-level 422 (#143's extra_forbidden shape included) it always
    did -- the detail is withheld from strangers, not from everyone."""
    secret = "route-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = json.dumps(
        {"source": "hmac-test", "sorce": "typo", "events": [_valid_event().model_dump(mode="json")]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    response = client.post(
        INGEST_URL,
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": _sign(secret, raw_body)},
    )

    assert response.status_code == 422, response.text
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


def test_route_401s_a_non_ascii_signature_header_instead_of_500(monkeypatch: Any) -> None:
    """hmac.compare_digest raises TypeError -- not False -- when a str
    argument carries a non-ASCII character, so a header like
    `sha256=café...` escaped as an unhandled 500 with a traceback, from an
    unauthenticated caller. It fails closed, so this is availability and
    information disclosure rather than a bypass, but a 500 is never the
    right answer to a bad signature (#209)."""
    secret = "route-shared-secret"
    monkeypatch.setenv(HMAC_ENV_VAR, secret)
    raw_body = _canonical_body()
    # Sent as raw bytes, because that is how the byte reaches a real server:
    # HTTP header octets are decoded latin-1 by the ASGI layer, so 0xE9
    # arrives at the route as "é" in a str. httpx refuses to ASCII-encode a
    # str header value, which is exactly why passing bytes here is not a
    # contrivance -- it is the only way to reproduce the real request.
    non_ascii_digest = ("caf\u00e9" + "0" * 60).encode("latin-1")

    response = client.post(
        INGEST_URL,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": b"sha256=" + non_ascii_digest,
        },
    )

    assert response.status_code == 401, response.text
    assert response.status_code != 500
    assert "signature" in response.json()["detail"].lower()


def test_service_rejects_a_non_ascii_signature_without_raising_type_error(
    monkeypatch: Any,
) -> None:
    """Same defect at the service level, where the TypeError originates."""
    monkeypatch.setenv(HMAC_ENV_VAR, "unit-test-shared-secret")
    raw_body = _canonical_body()
    payload = IngestPayload.model_validate_json(raw_body)
    service = MockRegEngineService()

    with pytest.raises(MockRegEngineHTTPError) as exc_info:
        service.ingest(payload, raw_body=raw_body, signature_header="sha256=café")

    assert exc_info.value.status_code == 401

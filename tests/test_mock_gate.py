"""What the mock ingest route does BEFORE it trusts a caller.

Three separate ways the gate leaked, all fixed here and pinned so they stay
fixed:

* HMAC was not the outermost gate. `payload: IngestPayload` was a handler
  parameter, so FastAPI parsed and validated the whole batch before
  `verify_signature` ever ran. An unauthenticated caller got a full pydantic
  parse of an arbitrarily large batch, and on a malformed one a 422 that
  enumerated every accepted CTE type — a schema oracle live RegEngine never
  opens, because live verifies the signature first.
* A non-ASCII `X-Webhook-Signature` reached `hmac.compare_digest`, whose
  `TypeError` on non-ASCII str operands is not a `MockRegEngineHTTPError` and
  escaped the route as an unauthenticated 500 with a traceback.
* An unrecognised `X-Mock-Friction` code was passed through the router and
  ignored by `ingest`, so an operator rehearsing a 402 against a typo'd code
  got a clean 200 and concluded the failure mode was handled. Surfacing that
  exact class of silent no-op is what this simulator is for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.mock_service import (
    FRICTION_RESPONSES,
    MAX_BATCH_EVENTS,
    MAX_INGEST_BODY_BYTES,
    MockRegEngineHTTPError,
    MockRegEngineService,
    parse_ingest_payload,
    validate_friction_codes,
)
from app.regengine_client import WEBHOOK_HMAC_SECRET_ENV
from app.schemas.domain import CTEType
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import MockFrictionCode, SimulationConfig


# `raise_server_exceptions=False` so an unhandled handler exception surfaces as
# the 500 a real client would see, instead of being re-raised into the test.
client = TestClient(app, raise_server_exceptions=False)

HMAC_SECRET = "mock-gate-shared-secret"
WRONG_SIGNATURE = "sha256=" + "0" * 64


def setup_function() -> None:
    import asyncio

    asyncio.run(controller.reset(SimulationConfig()))


BASE_MOMENT = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)


def harvesting_event(lot_code: str = "TLC-GATE-000001") -> dict:
    return {
        "cte_type": "harvesting",
        "traceability_lot_code": lot_code,
        "product_description": "Romaine Lettuce",
        "quantity": 500,
        "unit_of_measure": "cases",
        "location_name": "Valley Fresh Farms",
        "timestamp": BASE_MOMENT.isoformat().replace("+00:00", "Z"),
        "kdes": {
            "harvest_date": BASE_MOMENT.date().isoformat(),
            "reference_document": "Harvest Record HR-GATE-0001",
        },
    }


def payload_dict(*events: dict, source: str = "mock-gate") -> dict:
    return {"source": source, "events": list(events)}


def canonical_body(payload: dict) -> bytes:
    return json.dumps(
        IngestPayload.model_validate(payload).model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def signature_for(body: bytes, secret: str = HMAC_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post_signed(body: dict, *, secret: str = HMAC_SECRET, **headers: str):
    signed = canonical_body(body)
    return client.post(
        "/api/mock/regengine/ingest",
        content=signed,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_for(signed, secret=secret),
            **headers,
        },
    )


# --- HMAC is the outermost gate -----------------------------------------------


def test_a_bad_signature_never_reveals_the_accepted_cte_vocabulary(monkeypatch) -> None:
    """The schema oracle: 422 used to list every CTEType to a 401 caller."""
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        json={"events": [{"cte_type": "bogus"}]},
        headers={"X-Webhook-Signature": WRONG_SIGNATURE},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    body = response.text
    for cte in CTEType:
        assert cte.value not in body


def test_a_missing_signature_never_reveals_the_required_field_names(monkeypatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post("/api/mock/regengine/ingest", json={"events": [{}]})
    assert response.status_code == 401
    assert response.json() == {"detail": "Missing X-Webhook-Signature header"}
    assert "traceability_lot_code" not in response.text


def test_an_unsigned_caller_does_not_get_the_batch_parsed(monkeypatch) -> None:
    """The parse-amplification half: pydantic must not run before the 401.

    `IngestPayload.events` deliberately carries no `max_length` (the 1-500 cap
    lives in `MockRegEngineService.ingest`, after verification), so a parse
    that happens before the signature check is bounded only by body size.
    """
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    import app.schemas.domain as domain

    built: list[str] = []
    original = domain.RegEngineEvent.__init__

    def counting(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        built.append("event")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(domain.RegEngineEvent, "__init__", counting)
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(*(harvesting_event(f"TLC-GATE-{i:06d}") for i in range(50))),
        headers={"X-Webhook-Signature": WRONG_SIGNATURE},
    )
    assert response.status_code == 401
    assert built == []


def test_a_correctly_signed_caller_still_gets_a_parsed_batch(monkeypatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = post_signed(payload_dict(harvesting_event("TLC-GATE-SIGNED-1")))
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_an_unset_secret_still_leaves_the_route_wide_open_by_design(monkeypatch) -> None:
    """The pre-signing ramp: no secret configured means no gate to fail."""
    monkeypatch.delenv(WEBHOOK_HMAC_SECRET_ENV, raising=False)
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-NOSECRET-1")),
    )
    assert response.status_code == 200


# --- the malformed-body 422 the mock now owns ---------------------------------


def test_a_signed_but_malformed_body_gets_the_mocks_own_string_detail(monkeypatch) -> None:
    """`detail` is a string, not FastAPI's list of error dicts.

    The mock already reports RegEngine's request-fatal field constraints as a
    plain string (`_batch_fatal_detail`, #101). Now that the route parses the
    body itself, a malformed body joins that shape instead of leaking
    FastAPI's internal error-dict list, so a client has ONE thing to code
    against for "the whole batch died before ingest".
    """
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    signed = b'{"source":"mock-gate","events":[{"cte_type":"bogus"}]}'
    response = client.post(
        "/api/mock/regengine/ingest",
        content=signed,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_for(signed),
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "nothing was stored" in detail
    assert "events.0.cte_type" in detail


def test_unparseable_json_is_a_422_not_a_500() -> None:
    response = client.post(
        "/api/mock/regengine/ingest",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_parse_ingest_payload_round_trips_a_good_body() -> None:
    raw = json.dumps(payload_dict(harvesting_event())).encode("utf-8")
    payload = parse_ingest_payload(raw)
    assert payload.source == "mock-gate"
    assert len(payload.events) == 1


def test_parse_ingest_payload_names_every_violation() -> None:
    raw = b'{"events":[{"cte_type":"bogus"},{"cte_type":"harvesting"}]}'
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        parse_ingest_payload(raw)
    assert excinfo.value.status_code == 422
    assert "events.0.cte_type" in excinfo.value.detail
    assert "events.1.traceability_lot_code" in excinfo.value.detail


def test_additive_fields_are_still_accepted_after_the_manual_parse() -> None:
    """The mock stands in for a receiver that must not 422 on a newer producer."""
    event = harvesting_event("TLC-GATE-ADDITIVE-1")
    event["some_future_field"] = "value"
    response = client.post("/api/mock/regengine/ingest", json=payload_dict(event))
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_the_route_still_publishes_the_ingest_payload_schema() -> None:
    """Dropping the body parameter must not drop the documented body."""
    schema = app.openapi()
    body = schema["paths"]["/api/mock/regengine/ingest"]["post"]["requestBody"]
    assert body["required"] is True
    payload_schema = body["content"]["application/json"]["schema"]
    assert set(payload_schema["properties"]) == {"source", "events"}
    event_schema = payload_schema["properties"]["events"]["items"]
    assert {"cte_type", "traceability_lot_code", "kdes"} <= set(event_schema["properties"])
    assert set(event_schema["properties"]["cte_type"]["enum"]) == {c.value for c in CTEType}


def test_the_published_schema_has_no_dangling_references() -> None:
    """`openapi_extra` is spliced in verbatim, so its `$ref`s must resolve."""
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    refs = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                refs.add(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    assert all(
        ref.startswith("#/components/schemas/")
        and ref.removeprefix("#/components/schemas/") in components
        for ref in refs
    )


# --- the body ceiling ---------------------------------------------------------


def test_an_oversized_body_is_refused_before_it_is_parsed() -> None:
    oversized = b'{"source":"mock-gate","events":[]}' + b" " * MAX_INGEST_BODY_BYTES
    response = client.post(
        "/api/mock/regengine/ingest",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert str(MAX_INGEST_BODY_BYTES) in detail
    assert str(MAX_BATCH_EVENTS) in detail


def test_the_ceiling_is_refused_on_content_length_alone(monkeypatch) -> None:
    """An oversized body must not have to arrive before it is refused."""
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(MAX_INGEST_BODY_BYTES + 1),
        },
    )
    assert response.status_code == 413


def test_the_ceiling_clears_a_full_500_event_batch() -> None:
    """The cap must only bite bodies no legitimate batch can reach."""
    events = [harvesting_event(f"TLC-GATE-CAP-{i:06d}") for i in range(MAX_BATCH_EVENTS)]
    body = json.dumps(payload_dict(*events)).encode("utf-8")
    assert len(body) < MAX_INGEST_BODY_BYTES
    response = client.post(
        "/api/mock/regengine/ingest",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == MAX_BATCH_EVENTS


# --- non-ASCII signature headers ----------------------------------------------


@pytest.mark.parametrize(
    "raw_header",
    [
        b"sha256=\xe9beef",
        b"\xff" * 64,
        b"sha256=" + b"\xc3\xa9" * 32,
    ],
)
def test_a_non_ascii_signature_header_is_a_401_not_a_500(monkeypatch, raw_header: bytes) -> None:
    """Starlette decodes headers as latin-1, so these reach the comparison.

    `hmac.compare_digest` raises TypeError on str operands with any character
    outside ASCII. That is not a MockRegEngineHTTPError, so it escaped the
    route handler as a 500 with a traceback — to a caller who never
    authenticated. Trivially scriptable noise.
    """
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-NONASCII-1")),
        headers={b"X-Webhook-Signature": raw_header},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}


@pytest.mark.parametrize(
    "candidate",
    [
        "é" * 64,
        "sha256=é" * 8,
        "not-hex-at-all",
        "sha256=",
        "",
        "0" * 63,
        "0" * 65,
        "sha256=" + "z" * 64,
    ],
)
def test_verify_signature_rejects_anything_that_is_not_a_hex_digest(
    monkeypatch, candidate: str
) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    service = MockRegEngineService()
    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        service.verify_signature(b'{"events":[]}', candidate)
    assert excinfo.value.status_code == 401


def test_a_malformed_signature_is_indistinguishable_from_a_wrong_one(monkeypatch) -> None:
    """Same wording either way: the format pre-check must not be an oracle."""
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    service = MockRegEngineService()
    details = set()
    for candidate in ("é" * 64, "0" * 64, "sha256=" + "0" * 64, "nonsense"):
        with pytest.raises(MockRegEngineHTTPError) as excinfo:
            service.verify_signature(b'{"events":[]}', candidate)
        details.add(excinfo.value.detail)
    assert details == {"Invalid webhook signature"}


def test_the_correct_digest_still_verifies_in_both_accepted_formats(monkeypatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    service = MockRegEngineService()
    body = b'{"events":[]}'
    prefixed = signature_for(body)
    service.verify_signature(body, prefixed)
    service.verify_signature(body, prefixed.removeprefix("sha256="))
    service.verify_signature(body, f"  {prefixed}  ")


# --- unknown friction codes ---------------------------------------------------


def test_an_unknown_friction_code_is_a_400_that_names_it() -> None:
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-FRICTION-1")),
        headers={"X-Mock-Friction": "rate_limitt"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "rate_limitt" in detail
    for code in FRICTION_RESPONSES:
        assert code in detail


def test_every_unknown_code_in_the_header_is_named() -> None:
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-FRICTION-2")),
        headers={"X-Mock-Friction": "rate_limitt,invalid-key,typo"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert all(code in detail for code in ("rate_limitt", "invalid-key", "typo"))


def test_one_bad_code_alongside_a_good_one_still_fails() -> None:
    """Otherwise the good code fires and the typo stays invisible."""
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-FRICTION-3")),
        headers={"X-Mock-Friction": "rate_limit,typo"},
    )
    assert response.status_code == 400
    assert "typo" in response.json()["detail"]


@pytest.mark.parametrize(("code", "status"), sorted((k, v[0]) for k, v in FRICTION_RESPONSES.items()))
def test_every_known_friction_code_still_fires(code: str, status: int) -> None:
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-FRICTION-OK")),
        headers={"X-Mock-Friction": code},
    )
    assert response.status_code == status


def test_a_blank_or_absent_friction_header_is_still_a_no_op() -> None:
    for header in ("", "  ", " , "):
        response = client.post(
            "/api/mock/regengine/ingest",
            json=payload_dict(harvesting_event("TLC-GATE-FRICTION-BLANK")),
            headers={"X-Mock-Friction": header},
        )
        assert response.status_code == 200


def test_friction_validation_runs_behind_the_signature_gate(monkeypatch) -> None:
    """An unauthenticated caller learns nothing, not even the code list."""
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        json=payload_dict(harvesting_event("TLC-GATE-FRICTION-4")),
        headers={"X-Webhook-Signature": WRONG_SIGNATURE, "X-Mock-Friction": "typo"},
    )
    assert response.status_code == 401
    assert "typo" not in response.text


def test_validate_friction_codes_passes_known_codes_through() -> None:
    known = tuple(sorted(FRICTION_RESPONSES))
    assert validate_friction_codes(known) == known
    assert validate_friction_codes(()) == ()


def test_the_header_and_the_typed_config_accept_the_same_codes() -> None:
    """`DeliveryConfig.mock_friction` is the in-process path's guard.

    It is a typed Literal, so pydantic already rejects a typo there. The HTTP
    header path had no equivalent; these two sets must stay identical or the
    two paths rehearse different failure modes.
    """
    assert set(FRICTION_RESPONSES) == set(MockFrictionCode.__args__)


# --- ordering of the whole gate -----------------------------------------------


def test_the_size_ceiling_precedes_the_signature_check(monkeypatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        content=b"{}" + b" " * MAX_INGEST_BODY_BYTES,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_the_signature_check_precedes_both_friction_and_parsing(monkeypatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = client.post(
        "/api/mock/regengine/ingest",
        content=b'{"events":[{"cte_type":"bogus"}]}',
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": WRONG_SIGNATURE,
            "X-Mock-Friction": "not-a-code",
        },
    )
    assert response.status_code == 401


def test_friction_precedes_ingest_so_a_typo_never_reaches_a_valid_batch(monkeypatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, HMAC_SECRET)
    response = post_signed(
        payload_dict(harvesting_event("TLC-GATE-ORDER-1")),
        **{"X-Mock-Friction": "typo"},
    )
    assert response.status_code == 400

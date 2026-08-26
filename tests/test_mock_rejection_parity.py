"""Where the mock draws its rejection boundaries, pinned against RegEngine.

Two drift classes live here.

#101 — RegEngine enforces four ``IngestEvent`` checks as Pydantic *field*
constraints (``traceability_lot_code`` min_length=3, ``product_description``
1-500, ``quantity`` gt=0, and the 24h future-timestamp ``@field_validator``).
FastAPI 422s the whole request body before the ingest handler runs, so one
bad event loses the entire batch. The mock used to report all four as
per-event rejections inside an HTTP 200, so a demo showed "9 accepted, 1
rejected" where live lost all 10. These tests pin the *classification*, not
just the check list, so the next constraint RegEngine adds is caught here.

#102 — RegEngine rejects any event older than ``WEBHOOK_MAX_EVENT_AGE_DAYS``
(default 90) with "replay window exceeded". That one is handler-level, so it
is a per-event rejection inside a 200. The mock models it, but defaults to
``REGENGINE_MOCK_EVENT_AGE_MODE=warn`` because the shipped demo fixtures
carry fixed 2026-02 timestamps that predate the window; see
``test_default_age_mode_is_warn_while_shipped_fixtures_predate_the_window``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.demo_fixtures import DEMO_FIXTURES
from app.engine import DEFAULT_HISTORY_HOURS, LegitFlowEngine
from app.main import app, controller
from app.mock_service import (
    BATCH_FATAL_FIELD_CHECKS,
    DEFAULT_EVENT_AGE_MODE,
    EVENT_AGE_MODE_ENV,
    EVENT_AGE_MODES,
    MAX_EVENT_AGE_DAYS,
    MAX_EVENT_AGE_DAYS_ENV,
    MAX_FUTURE_HOURS,
    PER_EVENT_HANDLER_CHECKS,
    MockRegEngineHTTPError,
    MockRegEngineService,
    event_age_mode,
    field_constraint_errors,
    handler_rejection_errors,
    max_event_age_days,
    replay_window_errors,
    validate_event_like_regengine,
)
from app.schemas.domain import RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig


client = TestClient(app)

INGEST_URL = "/api/mock/regengine/ingest"


def setup_function() -> None:
    asyncio.run(controller.reset(SimulationConfig()))


def receiving_event(lot_code: str = "TLC-REJPAR-000001", **overrides) -> dict:
    """A fully valid receiving event, timestamped inside the replay window."""
    moment = datetime.now(UTC) - timedelta(days=1)
    event = {
        "cte_type": "receiving",
        "traceability_lot_code": lot_code,
        "product_description": "Romaine Lettuce",
        "quantity": 500,
        "unit_of_measure": "cases",
        "location_name": "Distribution Center #4",
        "timestamp": moment.isoformat().replace("+00:00", "Z"),
        "kdes": {
            "receive_date": moment.date().isoformat(),
            "receiving_location": "Distribution Center #4",
            "ship_from_location": "Valley Fresh Farms",
            "immediate_previous_source": "Valley Fresh Farms",
            "reference_document": "Bill of Lading BOL-REJPAR-0001",
            "tlc_source_reference": "SRC-REJPAR-0001",
        },
    }
    event.update(overrides)
    return event


def payload_dict(*events: dict) -> dict:
    return {"source": "mock-rejection-parity", "events": list(events)}


def ingest_payload(*events: dict) -> IngestPayload:
    return IngestPayload.model_validate(payload_dict(*events))


def model_event(**overrides) -> RegEngineEvent:
    return RegEngineEvent.model_validate(receiving_event(**overrides))


def days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


# --- #101: field constraints are request-fatal, not per-event -----------------


FIELD_CONSTRAINT_CASES = (
    ("traceability_lot_code", {"traceability_lot_code": "AB"}),
    ("product_description", {"product_description": "x" * 501}),
    ("product_description", {"product_description": ""}),
    ("quantity", {"quantity": 0}),
    ("quantity", {"quantity": -5}),
    (
        "timestamp",
        {"timestamp": (datetime.now(UTC) + timedelta(hours=48)).isoformat().replace("+00:00", "Z")},
    ),
)


@pytest.mark.parametrize("field,override", FIELD_CONSTRAINT_CASES)
def test_regengine_field_constraints_kill_the_whole_batch(field: str, override: dict) -> None:
    """One bad field loses every event, exactly as a live 422 does."""
    service = MockRegEngineService()
    payload = ingest_payload(
        receiving_event("TLC-REJPAR-GOOD-1"),
        receiving_event("TLC-REJPAR-BAD-01", **override),
        receiving_event("TLC-REJPAR-GOOD-2"),
    )

    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        service.ingest(payload)

    assert excinfo.value.status_code == 422
    assert f"events.1.{field}" in excinfo.value.detail
    # Nothing was ingested: the chain never advanced past its empty head.
    assert service.chain_hash == ""


def test_batch_fatal_detail_names_every_violation_and_says_nothing_was_stored() -> None:
    service = MockRegEngineService()
    payload = ingest_payload(
        receiving_event("AB"),
        receiving_event("TLC-REJPAR-QTY", quantity=0),
    )

    with pytest.raises(MockRegEngineHTTPError) as excinfo:
        service.ingest(payload)

    detail = excinfo.value.detail
    assert "events.0.traceability_lot_code" in detail
    assert "events.1.quantity" in detail
    assert "entire batch of 2 event(s) was rejected and nothing was stored" in detail


def test_http_ingest_returns_422_rather_than_a_partial_success_body() -> None:
    response = client.post(
        INGEST_URL,
        json=payload_dict(
            receiving_event("TLC-REJPAR-HTTP-1"),
            receiving_event("A"),
        ),
    )

    assert response.status_code == 422
    body = response.json()
    # The reassuring "1 accepted, 1 rejected" shape must not appear at all.
    assert "accepted" not in body
    assert "events.1.traceability_lot_code" in body["detail"]


def test_field_constraint_422_precedes_idempotency_bookkeeping() -> None:
    """A 422 never reaches the handler, so it cannot burn an Idempotency-Key."""
    service = MockRegEngineService()
    with pytest.raises(MockRegEngineHTTPError):
        service.ingest(ingest_payload(receiving_event("AB")), idempotency_key="key-101")

    good = service.ingest(
        ingest_payload(receiving_event("TLC-REJPAR-IDEM-1")),
        idempotency_key="key-101",
    )
    assert good.accepted == 1


def test_handler_level_checks_stay_per_event_inside_a_200() -> None:
    """Location/KDE failures reject one event and leave the rest accepted."""
    response = client.post(
        INGEST_URL,
        json=payload_dict(
            receiving_event("TLC-REJPAR-PE-OK"),
            receiving_event("TLC-REJPAR-PE-BAD", kdes={}),
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 1
    assert body["events"][0]["status"] == "accepted"
    assert body["events"][1]["status"] == "rejected"


def test_classification_of_each_check_is_pinned() -> None:
    """The boundary itself, not just the check list.

    Sourced from RegEngine: ``webhook_models.py`` field constraints (fatal)
    versus ``webhook_router_v2/routes.py`` handler checks (per-event).
    """
    assert BATCH_FATAL_FIELD_CHECKS == (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "timestamp",
    )
    assert PER_EVENT_HANDLER_CHECKS == (
        "location",
        "required_kdes",
        "event_age",
        "duplicate_in_batch",
    )
    assert not set(BATCH_FATAL_FIELD_CHECKS) & set(PER_EVENT_HANDLER_CHECKS)


def test_field_constraint_errors_reports_only_field_level_failures() -> None:
    assert field_constraint_errors(model_event()) == []
    # Missing KDEs and a stale timestamp are handler concerns, not fatal ones.
    assert field_constraint_errors(model_event(kdes={}, timestamp=days_ago(400))) == []
    fields = [field for field, _ in field_constraint_errors(model_event(quantity=0))]
    assert fields == ["quantity"]


def test_handler_rejection_errors_reports_only_handler_failures() -> None:
    assert handler_rejection_errors(model_event()) == []
    # A short lot code is fatal upstream, so the handler view stays clean.
    assert handler_rejection_errors(model_event(traceability_lot_code="AB")) == []
    errors = handler_rejection_errors(model_event(location_name="", kdes={}))
    assert any("location identifier is required" in error for error in errors)


def test_validate_event_like_regengine_still_returns_the_union() -> None:
    """The full-parity helper other suites rely on keeps every reason."""
    event = model_event(quantity=0, kdes={}, timestamp=days_ago(400))
    errors = validate_event_like_regengine(event)
    assert "quantity must be greater than 0" in errors
    assert any("replay window exceeded" in error for error in errors)
    assert any("Missing required KDEs" in error for error in errors)


# --- #102: the 90-day replay window ------------------------------------------


def test_window_bounds_match_regengine_defaults() -> None:
    """Pinned against WEBHOOK_MAX_EVENT_AGE_DAYS / the 24h future ceiling."""
    assert MAX_EVENT_AGE_DAYS == 90
    assert MAX_FUTURE_HOURS == 24
    assert max_event_age_days() == 90


def test_events_older_than_the_window_carry_regengines_wording() -> None:
    errors = replay_window_errors(model_event(timestamp=days_ago(91)))
    assert len(errors) == 1
    assert "WEBHOOK_MAX_EVENT_AGE_DAYS=90" in errors[0]
    assert "replay window exceeded" in errors[0]


def test_events_inside_the_window_are_not_flagged() -> None:
    assert replay_window_errors(model_event(timestamp=days_ago(89))) == []
    assert replay_window_errors(model_event()) == []


def test_validate_event_like_regengine_rejects_out_of_window_events() -> None:
    """#102 acceptance: the parity validator knows an age floor exists."""
    errors = validate_event_like_regengine(model_event(timestamp=days_ago(120)))
    assert any("replay window exceeded" in error for error in errors)


def test_reject_mode_rejects_out_of_window_events_per_event_in_a_200(monkeypatch) -> None:
    """Live parity: an age failure is a per-event rejection, not a 422."""
    monkeypatch.setenv(EVENT_AGE_MODE_ENV, "reject")
    response = client.post(
        INGEST_URL,
        json=payload_dict(
            receiving_event("TLC-REJPAR-AGE-OK"),
            receiving_event("TLC-REJPAR-AGE-OLD", timestamp=days_ago(200)),
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 1
    assert any("replay window exceeded" in error for error in body["events"][1]["errors"])
    assert response.headers["X-Mock-Event-Age-Mode"] == "reject"


def test_warn_mode_accepts_but_reports_the_drift_on_the_response(monkeypatch) -> None:
    monkeypatch.setenv(EVENT_AGE_MODE_ENV, "warn")
    response = client.post(
        INGEST_URL,
        json=payload_dict(receiving_event("TLC-REJPAR-WARN", timestamp=days_ago(200))),
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.headers["X-Mock-Event-Age-Window-Days"] == "90"
    assert response.headers["X-Mock-Out-Of-Window-Events"] == "1"
    assert "replay window exceeded" in response.headers["X-Mock-Event-Age-Warning"]
    assert "X-Mock-Event-Age-Warning" in response.headers["Access-Control-Expose-Headers"]


def test_warn_mode_headers_are_latin1_encodable(monkeypatch) -> None:
    """RegEngine's wording has an em dash; HTTP headers are latin-1 only."""
    monkeypatch.setenv(EVENT_AGE_MODE_ENV, "warn")
    response = client.post(
        INGEST_URL,
        json=payload_dict(receiving_event("TLC-REJPAR-DASH", timestamp=days_ago(200))),
    )
    response.headers["X-Mock-Event-Age-Warning"].encode("latin-1")


def test_warn_mode_logs_every_out_of_window_event(monkeypatch, caplog) -> None:
    monkeypatch.setenv(EVENT_AGE_MODE_ENV, "warn")
    service = MockRegEngineService()
    with caplog.at_level(logging.WARNING, logger="app.mock_service"):
        response = service.ingest(
            ingest_payload(receiving_event("TLC-REJPAR-LOG", timestamp=days_ago(200)))
        )

    assert response.accepted == 1
    assert service.last_age_window_warnings and "replay window exceeded" in (
        service.last_age_window_warnings[0]
    )
    assert any("replay window exceeded" in record.getMessage() for record in caplog.records)


def test_off_mode_is_silent_and_accepting(monkeypatch) -> None:
    monkeypatch.setenv(EVENT_AGE_MODE_ENV, "off")
    response = client.post(
        INGEST_URL,
        json=payload_dict(receiving_event("TLC-REJPAR-OFF", timestamp=days_ago(200))),
    )

    assert response.json()["accepted"] == 1
    assert response.headers["X-Mock-Out-Of-Window-Events"] == "0"
    assert "X-Mock-Event-Age-Warning" not in response.headers


@pytest.mark.parametrize("raw,expected", [("", "warn"), ("nonsense", "warn"), ("REJECT", "reject")])
def test_event_age_mode_env_parsing(monkeypatch, raw: str, expected: str) -> None:
    monkeypatch.setenv(EVENT_AGE_MODE_ENV, raw)
    assert event_age_mode() == expected
    assert expected in EVENT_AGE_MODES


@pytest.mark.parametrize("raw,expected", [("30", 30), ("0", 90), ("junk", 90), ("", 90)])
def test_max_event_age_days_env_parsing(monkeypatch, raw: str, expected: int) -> None:
    monkeypatch.setenv(MAX_EVENT_AGE_DAYS_ENV, raw)
    assert max_event_age_days() == expected


# --- what the window does to the shipped demo ---------------------------------


def test_engine_default_history_stays_well_inside_the_window() -> None:
    """A normal simulated run must never trip the floor."""
    assert DEFAULT_HISTORY_HOURS / 24 < MAX_EVENT_AGE_DAYS
    engine = LegitFlowEngine(seed=204)
    for _ in range(60):
        event, _parents = engine.next_event()
        assert replay_window_errors(event) == [], event.timestamp
        assert field_constraint_errors(event) == [], event.timestamp


def test_default_age_mode_is_warn_while_shipped_fixtures_predate_the_window() -> None:
    """The reason enforcement is off by default, made explicit.

    ``app/demo_fixtures.py`` carries fixed 2026-02 timestamps, so a default
    of "reject" would make every demo-fixture load and the browser smoke fail
    against the mock. If the fixtures are ever rebased onto relative
    timestamps, this test stops constraining the default and the mock should
    switch to "reject" for true parity.
    """
    stale = [
        fixture_event.event
        for fixture in DEMO_FIXTURES.values()
        for fixture_event in fixture.events
        if replay_window_errors(fixture_event.event)
    ]
    if stale:
        # Asserts the shipped default only — an explicit
        # REGENGINE_MOCK_EVENT_AGE_MODE=reject run is opting in knowingly.
        assert DEFAULT_EVENT_AGE_MODE == "warn"

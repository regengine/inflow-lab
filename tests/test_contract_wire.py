"""Wire-contract regressions for the RegEngine boundary.

Covers the three places where what inflow-lab *says* and what RegEngine (or
inflow-lab's own graph) *reads* had drifted apart:

* #91 - transformation input lots were emitted only inside `kdes`, while
  RegEngine's `IngestEvent` reads them from a top-level field, so every live
  transformation landed with zero lineage links.
* #97 - rework lots entered processor inventory with no CTE of their own, so a
  later transformation declared an input the lineage graph could not anchor.
* #100 - the Test-connection probe reported "connected" for credentials whose
  every ingest would fail.
* #136 - `CSVImportRequest.csv_text` had no maximum size.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.engine import LegitFlowEngine
from app.main import app
from app.regengine_client import (
    INPUT_LOT_CODES_FIELD,
    SIGNATURE_MISMATCH_VERDICT,
    WEBHOOK_HMAC_SECRET_ENV,
    LiveRegEngineClient,
    build_wire_body,
)
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.ingestion import MAX_CSV_IMPORT_CHARS, CSVImportRequest, IngestPayload
from app.schemas.simulation import SimulationConfig
from app.store import EventStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def drain(engine: LegitFlowEngine, count: int) -> list[tuple[RegEngineEvent, list[str]]]:
    return [engine.next_event() for _ in range(count)]


def transformation_event(input_lot_codes: Any) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.TRANSFORMATION,
        traceability_lot_code="TLC-20260427-000009",
        product_description="Chopped Romaine",
        quantity=42,
        unit_of_measure="cases",
        location_name="Meridian Fresh Cut",
        timestamp=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        kdes={
            "transformation_date": "2026-04-27",
            "reference_document": "Batch Record BATCH-1",
            "input_traceability_lot_codes": input_lot_codes,
        },
    )


def receiving_event() -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.RECEIVING,
        traceability_lot_code="TLC-20260427-000001",
        product_description="Romaine Lettuce",
        quantity=500,
        unit_of_measure="cases",
        location_name="Distribution Center #4",
        timestamp=datetime(2026, 4, 27, 8, 30, tzinfo=UTC),
        kdes={
            "receive_date": "2026-04-27",
            "receiving_location": "Distribution Center #4",
            "immediate_previous_source": "Valley Fresh Farms",
            "reference_document": "Bill of Lading BOL-1",
            "tlc_source_reference": "SRC-1",
        },
    )


class _FakeIngestResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"accepted": 1}


class RecordingIngestClient:
    """Captures the exact body bytes the live client puts on the wire."""

    bodies: list[bytes] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "RecordingIngestClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(
        self,
        endpoint: str,
        *,
        headers: dict[str, str],
        content: bytes,
    ) -> _FakeIngestResponse:
        RecordingIngestClient.bodies.append(content)
        return _FakeIngestResponse()


def live_config() -> SimulationConfig:
    config = SimulationConfig()
    config.delivery.mode = DestinationMode.LIVE
    config.delivery.api_key = "test-api-key"
    config.delivery.tenant_id = "test-tenant-id"
    return config


def post_live(monkeypatch: pytest.MonkeyPatch, payload: IngestPayload) -> dict[str, Any]:
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", RecordingIngestClient)
    RecordingIngestClient.bodies = []
    asyncio.run(LiveRegEngineClient().ingest(payload, live_config()))
    return json.loads(RecordingIngestClient.bodies[0].decode("utf-8"))


# ---------------------------------------------------------------------------
# #91 - transformation input lots must reach RegEngine's top-level field
# ---------------------------------------------------------------------------


def test_wire_body_hoists_transformation_input_lots_to_the_top_level() -> None:
    codes = ["TLC-A", "TLC-B"]
    body = build_wire_body(IngestPayload(events=[transformation_event(codes)]))

    (event,) = body["events"]
    assert event[INPUT_LOT_CODES_FIELD] == codes
    # The kdes copy is what this repo's own lineage view, EPCIS export and the
    # mock read, so hoisting must add to the payload rather than move anything.
    assert event["kdes"]["input_traceability_lot_codes"] == codes


def test_live_ingest_signs_and_sends_the_hoisted_field(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = ["TLC-A", "TLC-B"]
    body = post_live(monkeypatch, IngestPayload(events=[transformation_event(codes)]))

    # The bytes asserted here are the bytes that were signed: the live client
    # serializes once and posts `content=`.
    (event,) = body["events"]
    assert event[INPUT_LOT_CODES_FIELD] == codes


def test_events_without_input_lots_keep_their_previous_wire_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = post_live(monkeypatch, IngestPayload(events=[receiving_event()]))

    (event,) = body["events"]
    assert set(event) == {
        "cte_type",
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "location_name",
        "location_gln",
        "timestamp",
        "kdes",
    }
    # RegEngine's IngestEvent defaults the field to None, so omitting it and
    # sending an explicit null are equivalent -- and omitting keeps every
    # non-transformation event byte-identical to what shipped before.
    assert INPUT_LOT_CODES_FIELD not in event


def test_wire_body_accepts_the_csv_importer_string_form() -> None:
    body = build_wire_body(IngestPayload(events=[transformation_event("TLC-A")]))
    (event,) = body["events"]
    assert event[INPUT_LOT_CODES_FIELD] == ["TLC-A"]


@pytest.mark.parametrize("declared", [[], ["", "   "], None, "  ", 17])
def test_wire_body_omits_the_field_when_no_input_lots_are_declared(declared: Any) -> None:
    body = build_wire_body(IngestPayload(events=[transformation_event(declared)]))
    (event,) = body["events"]
    assert INPUT_LOT_CODES_FIELD not in event


def test_every_generated_transformation_ships_its_inputs_where_regengine_reads_them() -> None:
    """Pin the *location* of transformation input lots on the wire.

    `tests/test_regengine_contract_pin.py` pins the required-KDE table but not
    payload shape, which is why a field-placement mismatch survived: the mock
    reads `kdes`, RegEngine reads the top-level field, and only live traffic
    could tell the difference. This fails if the two diverge again.
    """
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")
    generated = drain(engine, 400)
    events = [event for event, _ in generated]
    body = build_wire_body(IngestPayload(events=events))

    transformations = [
        (event, wire)
        for event, wire in zip(events, body["events"], strict=True)
        if event.cte_type == CTEType.TRANSFORMATION
    ]
    assert transformations, "expected the fresh-cut scenario to transform"
    for event, wire in transformations:
        declared = event.kdes["input_traceability_lot_codes"]
        assert declared
        assert wire[INPUT_LOT_CODES_FIELD] == declared


# ---------------------------------------------------------------------------
# #97 - a rework lot must carry a CTE of its own
# ---------------------------------------------------------------------------


def rework_batches(generated: list[tuple[RegEngineEvent, list[str]]]) -> dict[str, list[str]]:
    """Rework lot codes declared by each transformation batch."""
    batches: dict[str, list[str]] = {}
    for event, _ in generated:
        if event.cte_type != CTEType.TRANSFORMATION:
            continue
        rework = event.kdes.get("rework_traceability_lot_codes") or []
        if rework:
            batches[event.kdes["reference_document_number"]] = list(rework)
    return batches


def test_rework_lots_get_their_own_transformation_record() -> None:
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")
    generated = drain(engine, 400)

    batches = rework_batches(generated)
    assert batches, "expected the fresh-cut scenario to produce rework"

    recorded = {event.traceability_lot_code for event, _ in generated}
    declared_rework = {code for codes in batches.values() for code in codes}
    assert declared_rework <= recorded

    for event, parents in generated:
        if event.kdes.get("rework_output"):
            # A rework CTE hangs off the same inputs as its batch, so backward
            # lineage from the rework lot reaches the same source lots.
            assert event.cte_type == CTEType.TRANSFORMATION
            assert parents == event.kdes["input_traceability_lot_codes"]
            assert event.quantity > 0


def test_no_transformation_declares_an_input_lot_that_has_no_record() -> None:
    for scenario in ("fresh_cut_processor", "seafood_first_receiver"):
        engine = LegitFlowEngine(seed=204, scenario=scenario)
        generated = drain(engine, 400)
        recorded = {event.traceability_lot_code for event, _ in generated}
        for event, _ in generated:
            declared = event.kdes.get("input_traceability_lot_codes") or []
            missing = [code for code in declared if code not in recorded]
            assert not missing, (scenario, event.traceability_lot_code, missing)


def store_from(tmp_path: Any, generated: list[tuple[RegEngineEvent, list[str]]]) -> EventStore:
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.add_many(
        [
            StoredEventRecord(payload_source="test", event=event, parent_lot_codes=parents)
            for event, parents in generated
        ]
    )
    return store


def test_lineage_edges_cover_every_declared_input_of_a_rework_batch(tmp_path: Any) -> None:
    engine = LegitFlowEngine(seed=204, scenario="fresh_cut_processor")
    generated = drain(engine, 400)
    batches = rework_batches(generated)
    assert batches

    store = store_from(tmp_path, generated)

    # Find a transformation that consumed a rework lot minted by an earlier
    # batch -- the exact case that used to lose an edge.
    rework_codes = {code for codes in batches.values() for code in codes}
    consumers = [
        event
        for event, _ in generated
        if event.cte_type == CTEType.TRANSFORMATION
        and not event.kdes.get("rework_output")
        and rework_codes.intersection(event.kdes.get("input_traceability_lot_codes") or [])
    ]
    assert consumers, "expected a later batch to consume a rework lot"

    for event in consumers:
        records = store.lineage(event.traceability_lot_code)
        edges = store.lineage_edges(records)
        sources = {
            edge.source_lot_code
            for edge in edges
            if edge.target_lot_code == event.traceability_lot_code
        }
        assert set(event.kdes["input_traceability_lot_codes"]) <= sources


def test_seeded_runs_stay_deterministic() -> None:
    def signature(seed: int, scenario: str) -> list[tuple[str, str, float]]:
        engine = LegitFlowEngine(seed=seed, scenario=scenario)
        return [
            (event.cte_type.value, event.traceability_lot_code, event.quantity)
            for event, _ in drain(engine, 200)
        ]

    for scenario in ("fresh_cut_processor", "leafy_greens_supplier"):
        assert signature(204, scenario) == signature(204, scenario)


# ---------------------------------------------------------------------------
# #100 - the connection probe must not certify what it did not test
# ---------------------------------------------------------------------------


class _ProbeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class ProbeClient:
    health_payload: Any = None

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "ProbeClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _ProbeResponse:
        if url.endswith("/health"):
            return _ProbeResponse(200, ProbeClient.health_payload)
        return _ProbeResponse(200, {"events": []})


def probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health: Any,
    local_secret: str | None,
) -> Any:
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ProbeClient)
    ProbeClient.health_payload = health
    if local_secret is None:
        monkeypatch.delenv(WEBHOOK_HMAC_SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, local_secret)
    return asyncio.run(LiveRegEngineClient().check_connection(live_config()))


@pytest.mark.parametrize("key", ["webhook_hmac_configured", "hmac_configured"])
def test_probe_flags_regengine_requiring_signatures_the_simulator_cannot_send(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    result = probe(monkeypatch, health={"status": "healthy", key: True}, local_secret=None)

    # This is the config in which /recent returns 200 and every /ingest 401s.
    assert result.verdict == SIGNATURE_MISMATCH_VERDICT
    assert WEBHOOK_HMAC_SECRET_ENV in result.detail
    assert "401" in result.detail


def test_probe_flags_a_local_secret_regengine_does_not_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = probe(
        monkeypatch,
        health={"status": "healthy", "webhook_hmac_configured": False},
        local_secret="shared-secret",
    )
    assert result.verdict == SIGNATURE_MISMATCH_VERDICT


@pytest.mark.parametrize(
    ("health", "local_secret"),
    [
        ({"status": "healthy", "webhook_hmac_configured": True}, "shared-secret"),
        ({"status": "healthy", "webhook_hmac_configured": False}, None),
        # An older RegEngine that does not advertise its posture, and a
        # deployment serving a non-JSON /health: posture is unknown, and the
        # probe never invents a failure it cannot observe.
        ({"status": "healthy"}, "shared-secret"),
        ({"status": "healthy"}, None),
        (None, "shared-secret"),
    ],
)
def test_probe_stays_connected_when_signing_posture_agrees_or_is_unknown(
    monkeypatch: pytest.MonkeyPatch, health: Any, local_secret: str | None
) -> None:
    result = probe(monkeypatch, health=health, local_secret=local_secret)
    assert result.verdict == "connected"


def test_connected_detail_does_not_claim_ingest_was_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = probe(monkeypatch, health=None, local_secret=None)

    assert result.verdict == "connected"
    detail = result.detail.lower()
    # It must say what was actually proven (an authenticated read) ...
    assert "read" in detail
    # ... and name the write-path gates it did not exercise.
    assert "subscription" in detail
    assert "webhooks.ingest" in detail
    assert "rate limit" in detail
    # The old wording flatly certified the credentials; that is the claim the
    # false green was made of.
    assert "credentials and tenant are valid." not in detail


# ---------------------------------------------------------------------------
# #136 - csv_text needs an enforced maximum size
# ---------------------------------------------------------------------------


def test_csv_import_request_accepts_a_body_at_the_limit() -> None:
    request = CSVImportRequest(import_type="seed_lots", csv_text="x" * MAX_CSV_IMPORT_CHARS)
    assert len(request.csv_text) == MAX_CSV_IMPORT_CHARS


def test_csv_import_request_rejects_an_oversized_body() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CSVImportRequest(import_type="seed_lots", csv_text="x" * (MAX_CSV_IMPORT_CHARS + 1))
    assert "csv_text" in str(excinfo.value)


def test_csv_import_route_rejects_an_oversized_body_before_parsing() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "seed_lots",
            "csv_text": "x" * (MAX_CSV_IMPORT_CHARS + 1),
        },
    )
    assert response.status_code == 422

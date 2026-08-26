from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.schemas.simulation import SimulationConfig


# Regression tests for #143 (reject unknown/misplaced request body fields),
# #144 (record_ids: [] must retry nothing, not everything), and #145 (bound
# lineage/export response sizes). Kept in their own file rather than added
# to an existing one -- this batch of fixes must not edit any existing test
# file (strict per-file ownership across parallel workstreams).

client = TestClient(app)

_BASE_TIME = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def setup_function() -> None:
    # app/main.py's `controller` is a module-level singleton shared by every
    # test file in the process (same convention as tests/test_api.py's own
    # setup_function) -- reset it before each test so records left behind by
    # another test, or an earlier test in this file, can't leak in.
    asyncio.run(controller.reset(SimulationConfig()))


def _make_stored_record(
    lot_code: str,
    *,
    cte_type: CTEType = CTEType.HARVESTING,
    minutes: int = 0,
    parent_lot_codes: list[str] | None = None,
    kdes: dict[str, Any] | None = None,
    delivery_status: str = "generated",
    error: str | None = None,
) -> StoredEventRecord:
    """Build a record straight into the store, bypassing the simulator loop.

    Mirrors tests/test_store.py's make_record helper. Kept local instead of
    imported: test files are independent of each other, and this one owns
    nothing in test_store.py.
    """
    return StoredEventRecord(
        payload_source="robustness-suite",
        event=RegEngineEvent(
            cte_type=cte_type,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=_BASE_TIME + timedelta(minutes=minutes),
            kdes=kdes or {},
        ),
        parent_lot_codes=parent_lot_codes or [],
        destination_mode=DestinationMode.NONE,
        delivery_status=delivery_status,
        error=error,
    )


# ---------------------------------------------------------------------------
# #143 -- unrecognized or misnested request body fields return 422 instead
# of being silently dropped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, body",
    [
        pytest.param(
            "/api/mock/regengine/ingest",
            {
                "source": "robustness-suite",
                "events": [
                    {
                        "cte_type": "harvesting",
                        "traceability_lot_code": "TLC-EXTRA-000001",
                        "product_description": "Romaine Lettuce",
                        "quantity": 100,
                        "unit_of_measure": "cases",
                        "location_name": "Valley Fresh Farms",
                        "timestamp": "2026-03-01T08:00:00Z",
                        "kdes": {},
                    }
                ],
                # Misspelled -- previously silently ignored, so a caller who
                # made this typo would get a 200 with their real intent
                # (extra events, a different field) dropped on the floor.
                "eventz": [],
            },
            id="ingest-misspelled-extra-field",
        ),
        pytest.param(
            "/api/delivery/retry",
            # "record_id" (singular) is not a field -- previously this
            # parsed as an empty request and retried every failed record,
            # the opposite of a caller trying to target one record.
            {"record_id": ["abc"]},
            id="delivery-retry-misspelled-record-ids",
        ),
        pytest.param(
            "/api/simulate/replay",
            {"persist_paths": "data/events.jsonl"},
            id="replay-misspelled-persist-path",
        ),
        pytest.param(
            "/api/import/csv",
            {
                "import_type": "scheduled_events",
                "csv_text": "cte_type,traceability_lot_code\n",
                "type": "scheduled_events",
            },
            id="csv-import-stray-field",
        ),
        pytest.param(
            "/api/scenario-saves/leafy_greens_supplier",
            # The exact failure shape #143 was filed over: a misnamed
            # wrapper key around a config override, silently no-opping to
            # hard-coded defaults instead of applying (or rejecting) it.
            {"configuration": {"source": "should-not-apply"}},
            id="scenario-save-misnamed-config-wrapper",
        ),
        pytest.param(
            "/api/demo-fixtures/leafy_greens_trace/load",
            {"restart": True},
            id="demo-fixture-load-misspelled-reset",
        ),
    ],
)
def test_unknown_field_in_request_body_returns_422(path: str, body: dict[str, Any]) -> None:
    response = client.post(path, json=body)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(error["type"] == "extra_forbidden" for error in errors)


def test_ingest_with_well_formed_body_still_succeeds() -> None:
    # Paired with the parametrized rejections above: forbidding extras must
    # not make a correctly-shaped request to the same endpoint any harder.
    response = client.post(
        "/api/mock/regengine/ingest",
        json={
            "source": "robustness-suite",
            "events": [
                {
                    "cte_type": "harvesting",
                    "traceability_lot_code": "TLC-ROBUST-000001",
                    "product_description": "Romaine Lettuce",
                    "quantity": 100,
                    "unit_of_measure": "cases",
                    "location_name": "Valley Fresh Farms",
                    "timestamp": "2026-03-01T08:00:00Z",
                    "kdes": {"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-ROBUST-001"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["events"][0]["status"] == "accepted"


# ---------------------------------------------------------------------------
# #144 -- record_ids: [] means "retry nothing"; omitting record_ids entirely
# still means "retry every failed record up to limit".
# ---------------------------------------------------------------------------


def test_retry_with_explicit_empty_record_ids_retries_nothing() -> None:
    failed = _make_stored_record(
        "TLC-RETRY-EMPTY-001",
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-EMPTY-001"},
        delivery_status="failed",
        error="temporary outage",
    )
    controller.store.add_many([failed])

    response = client.post(
        "/api/delivery/retry",
        json={"record_ids": [], "delivery": {"mode": "mock"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["requested"] == 0
    assert body["retryable"] == 0
    assert body["attempted"] == 0
    assert body["record_ids"] == []

    # The failed record must be untouched -- not silently retried.
    events = client.get("/api/events?limit=10").json()["events"]
    retried = next(event for event in events if event["record_id"] == failed.record_id)
    assert retried["delivery_status"] == "failed"
    assert retried["delivery_attempts"] == 0


def test_retry_with_omitted_record_ids_retries_all_failed_records() -> None:
    first = _make_stored_record(
        "TLC-RETRY-ALL-001",
        minutes=0,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-ALL-001"},
        delivery_status="failed",
        error="temporary outage",
    )
    second = _make_stored_record(
        "TLC-RETRY-ALL-002",
        minutes=1,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-ALL-002"},
        delivery_status="failed",
        error="temporary outage",
    )
    controller.store.add_many([first, second])

    # record_ids omitted entirely -- distinct from record_ids: [] above.
    response = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["requested"] == 2
    assert body["retryable"] == 2
    assert body["attempted"] == 2
    assert body["posted"] == 2
    assert set(body["record_ids"]) == {first.record_id, second.record_id}


def test_retry_with_nonempty_record_ids_still_filters_to_that_subset() -> None:
    # Guards against a fix that special-cases [] so broadly it breaks the
    # ordinary filtered-subset path.
    target = _make_stored_record(
        "TLC-RETRY-FILTER-TARGET",
        minutes=0,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-FILTER-001"},
        delivery_status="failed",
        error="temporary outage",
    )
    other = _make_stored_record(
        "TLC-RETRY-FILTER-OTHER",
        minutes=1,
        kdes={"harvest_date": "2026-03-01", "reference_document": "Harvest Log HL-FILTER-002"},
        delivery_status="failed",
        error="temporary outage",
    )
    controller.store.add_many([target, other])

    response = client.post(
        "/api/delivery/retry",
        json={"record_ids": [target.record_id], "delivery": {"mode": "mock"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 1
    assert body["attempted"] == 1
    assert body["record_ids"] == [target.record_id]

    events = client.get("/api/events?limit=10").json()["events"]
    other_record = next(event for event in events if event["record_id"] == other.record_id)
    assert other_record["delivery_status"] == "failed"  # untouched


# ---------------------------------------------------------------------------
# #145 -- lineage and export endpoint response sizes are bounded.
# ---------------------------------------------------------------------------


def test_lineage_truncates_past_limit_and_signals_via_headers() -> None:
    records = [
        _make_stored_record("TLC-CHAIN-0", cte_type=CTEType.HARVESTING, minutes=0),
        _make_stored_record(
            "TLC-CHAIN-1", cte_type=CTEType.INITIAL_PACKING, minutes=1, parent_lot_codes=["TLC-CHAIN-0"]
        ),
        _make_stored_record(
            "TLC-CHAIN-2", cte_type=CTEType.COOLING, minutes=2, parent_lot_codes=["TLC-CHAIN-1"]
        ),
        _make_stored_record(
            "TLC-CHAIN-3", cte_type=CTEType.SHIPPING, minutes=3, parent_lot_codes=["TLC-CHAIN-2"]
        ),
        _make_stored_record(
            "TLC-CHAIN-4", cte_type=CTEType.RECEIVING, minutes=4, parent_lot_codes=["TLC-CHAIN-3"]
        ),
    ]
    controller.store.add_many(records)

    response = client.get("/api/lineage/TLC-CHAIN-4?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert len(body["records"]) == 2
    # An oldest-first prefix of the full match, not an arbitrary subset.
    assert [record["event"]["traceability_lot_code"] for record in body["records"]] == [
        "TLC-CHAIN-0",
        "TLC-CHAIN-1",
    ]
    assert response.headers["X-Lineage-Total-Matched"] == "5"
    assert response.headers["X-Lineage-Truncated"] == "true"


def test_lineage_under_limit_is_not_marked_truncated() -> None:
    records = [
        _make_stored_record("TLC-SMALL-0", minutes=0),
        _make_stored_record("TLC-SMALL-1", minutes=1, parent_lot_codes=["TLC-SMALL-0"]),
    ]
    controller.store.add_many(records)

    response = client.get("/api/lineage/TLC-SMALL-1")

    assert response.status_code == 200
    assert len(response.json()["records"]) == 2
    assert response.headers["X-Lineage-Total-Matched"] == "2"
    assert response.headers["X-Lineage-Truncated"] == "false"


@pytest.mark.parametrize("limit", [0, 1001])
def test_lineage_limit_query_param_rejects_out_of_bounds_values(limit: int) -> None:
    # Query validation runs before the handler body, so this 422s even for a
    # lot code that does not exist.
    response = client.get(f"/api/lineage/TLC-DOES-NOT-EXIST?limit={limit}")

    assert response.status_code == 422


def test_events_endpoint_limit_still_refuses_past_its_existing_maximum() -> None:
    # /api/events already bounded its response before this batch of fixes
    # (Query(..., le=500)). Documented here alongside /api/lineage's new
    # bound so the "a bounded endpoint refuses past its maximum" behavior
    # this batch adds is proven for both endpoints in this router.
    response = client.get("/api/events?limit=501")

    assert response.status_code == 422

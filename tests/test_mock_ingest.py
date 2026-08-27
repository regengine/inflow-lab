"""The mock RegEngine ingest endpoint: accepted-event hashing and the
KDE validation that mirrors live RegEngine (split out of
tests/test_api.py for #132).
"""

from tests.support.api_client import client, reset_app_state


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_mock_ingest_endpoint_returns_hashes():
    payload = {
        "source": "test-suite",
        "events": [
            {
                "cte_type": "receiving",
                "traceability_lot_code": "TLC-TEST-000001",
                "product_description": "Romaine Lettuce",
                "quantity": 500,
                "unit_of_measure": "cases",
                "location_name": "Distribution Center #4",
                "timestamp": "2026-02-05T08:30:00Z",
                "kdes": {
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                    "immediate_previous_source": "Valley Fresh Farms",
                    "reference_document": "Bill of Lading BOL-TEST-0001",
                    "tlc_source_reference": "SRC-TEST-0001",
                },
            }
        ],
    }
    response = client.post("/api/mock/regengine/ingest", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["events"][0]["status"] == "accepted"
    assert body["events"][0]["sha256_hash"]
    assert body["events"][0]["chain_hash"]


def test_mock_ingest_rejects_events_missing_required_kdes_like_regengine():
    payload = {
        "source": "test-suite",
        "events": [
            {
                "cte_type": "receiving",
                "traceability_lot_code": "TLC-TEST-REJECT-01",
                "product_description": "Romaine Lettuce",
                "quantity": 500,
                "unit_of_measure": "cases",
                "location_name": "Distribution Center #4",
                "timestamp": "2026-02-05T08:30:00Z",
                # reference_document_type/number deliberately do NOT satisfy
                # the combined reference_document key: the live validator uses
                # strict string lookup, and the mock must match it.
                "kdes": {
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "reference_document_type": "Bill of Lading",
                    "reference_document_number": "BOL-TEST-0002",
                },
            }
        ],
    }
    response = client.post("/api/mock/regengine/ingest", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 0
    assert body["rejected"] == 1
    event_result = body["events"][0]
    assert event_result["status"] == "rejected"
    assert event_result["event_id"] is None
    assert event_result["sha256_hash"] is None
    joined_errors = " ".join(event_result["errors"])
    assert "immediate_previous_source" in joined_errors
    assert "reference_document" in joined_errors
    assert "tlc_source_reference" in joined_errors

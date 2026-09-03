"""Live RegEngine delivery: credential redaction, missing-credential
rejection, failure/retry feedback, idempotency-key reuse and API-key
masking (split out of tests/test_api.py for #132).
"""

import json

from app.main import controller
from app.regengine_client import LiveIngestResult, LiveRegEngineDeliveryError
from tests.support.api_client import client, reset_app_state


def assert_json_omits(payload: object, *needles: str) -> None:
    dumped = json.dumps(payload, sort_keys=True)
    for needle in needles:
        assert needle not in dumped


def setup_function() -> None:
    # reset shared app state between tests
    reset_app_state()


def test_status_surfaces_redact_live_delivery_credentials():
    api_key = "regengine-live-api-key-secret"
    tenant_id = "regengine-live-tenant-secret"
    reset_response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "delivery": {
                "mode": "live",
                "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
                "api_key": api_key,
                "tenant_id": tenant_id,
            },
        },
    )
    assert reset_response.status_code == 200

    status = client.get("/api/simulate/status").json()
    health = client.get("/api/health").json()
    stream_response = client.get("/api/simulate/stream?limit=5&once=true")
    stream_data = next(
        line.removeprefix("data: ")
        for line in stream_response.text.splitlines()
        if line.startswith("data: ")
    )
    snapshot = json.loads(stream_data)

    for payload in (status, health, snapshot):
        assert_json_omits(payload, api_key, tenant_id)

    delivery = status["config"]["delivery"]
    assert delivery["mode"] == "live"
    assert delivery["endpoint"] == "https://www.regengine.co/api/v1/webhooks/ingest"
    assert delivery["api_key"] is None
    assert delivery["tenant_id"] is None
    assert health["status"]["config"]["delivery"] == delivery
    assert snapshot["status"]["config"]["delivery"] == delivery


def test_start_rejects_live_delivery_without_credentials(tmp_path):
    custom_path = tmp_path / "start-live-missing-creds.jsonl"
    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
                "delivery": {"mode": "live"},
            }
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Live delivery requires both api_key and tenant_id"
    assert client.get("/api/simulate/status").json()["running"] is False
    assert not custom_path.exists()


def test_live_delivery_operations_reject_missing_credentials(tmp_path):
    custom_path = tmp_path / "live-missing-creds-events.jsonl"
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
            "delivery": {"mode": "live"},
        },
    )

    operations = [
        client.post("/api/simulate/step"),
        client.post("/api/simulate/replay"),
        client.post(
            "/api/demo-fixtures/fresh_cut_transformation/load",
            json={"delivery": {"mode": "live"}},
        ),
        client.post(
            "/api/import/csv",
            json={
                "import_type": "seed_lots",
                "csv_text": "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\nTLC-LIVE,Romaine,1,cases,Farm",
                "delivery": {"mode": "live"},
            },
        ),
    ]

    for response in operations:
        assert response.status_code == 400
        assert response.json()["detail"] == "Live delivery requires both api_key and tenant_id"


def test_failed_live_delivery_surfaces_retry_feedback_and_can_retry_to_mock(tmp_path):
    class FailingLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            raise LiveRegEngineDeliveryError(
                "temporary outage",
                {
                    "delivery_mode": "live",
                    "endpoint_host": "www.regengine.co",
                    "endpoint_path": "/api/v1/webhooks/ingest",
                    "idempotency_key": "idem-failure-123",
                    "status_code": 503,
                },
            )

    original_live_client = controller.live_client
    controller.live_client = FailingLiveClient()
    custom_path = tmp_path / "failed-delivery-events.jsonl"
    try:
        client.post(
            "/api/simulate/reset",
            json={
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
                "delivery": {
                    "mode": "live",
                    "api_key": "live-api-secret",
                    "tenant_id": "live-tenant-secret",
                },
            },
        )

        step_response = client.post("/api/simulate/step")
    finally:
        controller.live_client = original_live_client

    assert step_response.status_code == 200
    step_body = step_response.json()
    assert step_body["generated"] == 1
    assert step_body["posted"] == 0
    assert step_body["failed"] == 1
    assert step_body["delivery_status"] == "failed"
    assert step_body["delivery_mode"] == "live"
    assert step_body["delivery_attempts"] == 1
    assert "temporary outage" in step_body["error"]

    status = client.get("/api/simulate/status").json()
    assert status["stats"]["delivery"]["failed"] == 1
    assert status["stats"]["delivery"]["retryable"] == 1
    assert status["stats"]["delivery"]["attempts"] == 1
    assert "temporary outage" in status["stats"]["delivery"]["last_error"]

    failed_record = client.get("/api/events?limit=1").json()["events"][0]
    assert failed_record["delivery_status"] == "failed"
    assert failed_record["delivery_attempts"] == 1
    assert failed_record["last_delivery_attempt_at"]
    assert failed_record["last_delivery_success_at"] is None

    retry_response = client.post("/api/delivery/retry", json={"delivery": {"mode": "mock"}})

    assert retry_response.status_code == 200
    retry_body = retry_response.json()
    assert retry_body["status"] == "posted"
    assert retry_body["requested"] == 1
    assert retry_body["retryable"] == 1
    assert retry_body["attempted"] == 1
    assert retry_body["posted"] == 1
    assert retry_body["failed"] == 0
    assert retry_body["delivery_mode"] == "mock"
    assert retry_body["record_ids"] == [failed_record["record_id"]]

    events = client.get("/api/events?limit=10").json()["events"]
    assert len(events) == 1
    retried_record = events[0]
    assert retried_record["record_id"] == failed_record["record_id"]
    assert retried_record["delivery_status"] == "posted"
    assert retried_record["destination_mode"] == "mock"
    assert retried_record["delivery_attempts"] == 2
    assert retried_record["last_delivery_success_at"]
    assert retried_record["error"] is None


def test_live_delivery_retry_reuses_original_idempotency_key(monkeypatch, tmp_path):
    class FlakyLiveClient:
        def __init__(self) -> None:
            self.idempotency_keys = []

        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            assert idempotency_key
            self.idempotency_keys.append(idempotency_key)
            metadata = {
                "delivery_mode": "live",
                "endpoint_host": "www.regengine.co",
                "endpoint_path": "/api/v1/webhooks/ingest",
                "idempotency_key": idempotency_key,
                "status_code": 503 if len(self.idempotency_keys) == 1 else 200,
            }
            if len(self.idempotency_keys) == 1:
                raise LiveRegEngineDeliveryError("connection dropped after upstream receive", metadata)
            return LiveIngestResult(
                response={
                    "accepted": len(payload.events),
                    "events": [
                        {
                            "traceability_lot_code": event.traceability_lot_code,
                            "status": "accepted",
                        }
                        for event in payload.events
                    ],
                },
                metadata=metadata,
            )

    flaky_client = FlakyLiveClient()
    monkeypatch.setattr(controller, "live_client", flaky_client)
    reset_response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(tmp_path / "live-retry-events.jsonl"),
            "delivery": {
                "mode": "live",
                "api_key": "live-api-secret",
                "tenant_id": "live-tenant-secret",
            },
        },
    )
    assert reset_response.status_code == 200

    step_response = client.post("/api/simulate/step")

    assert step_response.status_code == 200
    assert step_response.json()["delivery_status"] == "failed"
    failed_record = client.get("/api/events?limit=1").json()["events"][0]
    original_key = failed_record["delivery_metadata"]["idempotency_key"]
    assert original_key == flaky_client.idempotency_keys[0]

    retry_response = client.post("/api/delivery/retry")

    assert retry_response.status_code == 200
    retry_body = retry_response.json()
    assert retry_body["status"] == "posted"
    assert retry_body["posted"] == 1
    assert retry_body["failed"] == 0
    assert flaky_client.idempotency_keys == [original_key, original_key]

    retried_record = client.get("/api/events?limit=1").json()["events"][0]
    assert retried_record["delivery_status"] == "posted"
    assert retried_record["delivery_attempts"] == 2
    assert retried_record["delivery_metadata"]["idempotency_key"] == original_key


def test_successful_live_delivery_records_sanitized_audit_metadata(monkeypatch, tmp_path):
    class FakeLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            return LiveIngestResult(
                response={
                    "accepted": len(payload.events),
                    "events": [
                        {
                            "traceability_lot_code": event.traceability_lot_code,
                            "status": "accepted",
                        }
                        for event in payload.events
                    ],
                },
                metadata={
                    "delivery_mode": "live",
                    "endpoint_host": "www.regengine.co",
                    "endpoint_path": "/api/v1/webhooks/ingest",
                    "idempotency_key": "idem-test-123",
                    "status_code": 200,
                },
            )

    monkeypatch.setattr(controller, "live_client", FakeLiveClient())
    custom_path = tmp_path / "live-audit-events.jsonl"
    reset_response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
            "delivery": {
                "mode": "live",
                "api_key": "live-api-secret",
                "tenant_id": "live-tenant-secret",
            },
        },
    )
    assert reset_response.status_code == 200

    step_response = client.post("/api/simulate/step")

    assert step_response.status_code == 200
    assert step_response.json()["delivery_status"] == "posted"
    record = client.get("/api/events?limit=1").json()["events"][0]
    assert record["delivery_metadata"] == {
        "delivery_mode": "live",
        "endpoint_host": "www.regengine.co",
        "endpoint_path": "/api/v1/webhooks/ingest",
        "idempotency_key": "idem-test-123",
        "status_code": 200,
        "attempted_event_count": 1,
    }
    assert_json_omits(record["delivery_metadata"], "live-api-secret", "live-tenant-secret")


def test_failed_live_delivery_masks_api_key_in_error_message(tmp_path):
    api_key = "live-api-secret-leak"

    class LeakyLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            raise LiveRegEngineDeliveryError(
                f"upstream rejected request with key={api_key} for tenant",
                {
                    "delivery_mode": "live",
                    "endpoint_host": "www.regengine.co",
                    "endpoint_path": "/api/v1/webhooks/ingest",
                    "idempotency_key": "idem-leak-1",
                    "status_code": 401,
                },
            )

    original_live_client = controller.live_client
    controller.live_client = LeakyLiveClient()
    try:
        client.post(
            "/api/simulate/reset",
            json={
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(tmp_path / "leak-events.jsonl"),
                "delivery": {
                    "mode": "live",
                    "api_key": api_key,
                    "tenant_id": "live-tenant",
                },
            },
        )
        step_response = client.post("/api/simulate/step")
    finally:
        controller.live_client = original_live_client

    assert step_response.status_code == 200
    body = step_response.json()
    assert body["delivery_status"] == "failed"
    assert "***MASKED***" in body["error"]
    assert api_key not in body["error"]

    record = client.get("/api/events?limit=1").json()["events"][0]
    assert_json_omits(record, api_key)


def test_successful_live_delivery_masks_api_key_echoed_in_response(monkeypatch, tmp_path):
    api_key = "live-api-secret-echo"

    class EchoLiveClient:
        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            return LiveIngestResult(
                response={
                    "accepted": len(payload.events),
                    "events": [
                        {
                            "traceability_lot_code": event.traceability_lot_code,
                            "status": "accepted",
                        }
                        for event in payload.events
                    ],
                    "echoed_api_key": api_key,
                    "debug_message": f"received key {api_key}",
                },
                metadata={
                    "delivery_mode": "live",
                    "endpoint_host": "www.regengine.co",
                    "endpoint_path": "/api/v1/webhooks/ingest",
                    "idempotency_key": "idem-echo-1",
                    "status_code": 200,
                },
            )

    monkeypatch.setattr(controller, "live_client", EchoLiveClient())
    client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(tmp_path / "echo-events.jsonl"),
            "delivery": {
                "mode": "live",
                "api_key": api_key,
                "tenant_id": "live-tenant",
            },
        },
    )
    step_response = client.post("/api/simulate/step")

    assert step_response.status_code == 200
    assert step_response.json()["delivery_status"] == "posted"
    record = client.get("/api/events?limit=1").json()["events"][0]
    assert_json_omits(record, api_key)


def test_delivery_retry_empty_when_no_failed_records():
    response = client.post("/api/delivery/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["requested"] == 0
    assert body["retryable"] == 0
    assert body["attempted"] == 0
    assert body["record_ids"] == []

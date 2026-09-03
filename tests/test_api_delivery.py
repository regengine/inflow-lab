"""Live delivery configuration, credential handling and retry.

Moved verbatim out of ``test_api.py`` (see #132), plus the retry coverage
added for #163 at the end of the module.
"""

import asyncio
import base64
import json

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.simulation import SimulationConfig
from app.regengine_client import LiveIngestResult, LiveRegEngineDeliveryError


client = TestClient(app)

SECRET = "regengine-live-secret"
PUBLIC_ENDPOINT = "https://www.regengine.co/api/v1/webhooks/ingest"


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def assert_json_omits(payload: object, *needles: str) -> None:
    dumped = json.dumps(payload, sort_keys=True)
    for needle in needles:
        assert needle not in dumped


def setup_function() -> None:
    # reset shared app state between tests

    asyncio.run(controller.reset(SimulationConfig()))


def test_start_applies_configured_persist_path_and_keeps_mock_default(tmp_path):
    custom_path = tmp_path / "start-events.jsonl"
    response = client.post(
        "/api/simulate/start",
        json={
            "config": {
                "interval_seconds": 0.01,
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(custom_path),
            }
        },
    )
    try:
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["persist_path"] == str(custom_path)
        assert body["config"]["delivery"]["mode"] == "mock"
        assert body["stats"]["persist_path"] == str(custom_path)
    finally:
        client.post("/api/simulate/stop")


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


def test_reset_applies_configured_persist_path_for_next_step(tmp_path):
    custom_path = tmp_path / "reset-events.jsonl"
    response = client.post(
        "/api/simulate/reset",
        json={
            "batch_size": 1,
            "seed": 204,
            "persist_path": str(custom_path),
        },
    )
    assert response.status_code == 200

    status = client.get("/api/simulate/status").json()
    assert status["config"]["persist_path"] == str(custom_path)
    assert status["config"]["delivery"]["mode"] == "mock"
    assert status["stats"]["persist_path"] == str(custom_path)

    step_response = client.post("/api/simulate/step")
    assert step_response.status_code == 200
    assert step_response.json()["generated"] == 1
    assert custom_path.exists()
    assert len(custom_path.read_text(encoding="utf-8").splitlines()) == 1


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


# --- Retry's less-travelled branches (#163) -------------------------------
#
# Every retry test above exercises the full, unscoped failed set and asserts
# `status == "posted"`. The branches below -- `none`-mode "skipped", a mixed
# batch reporting "partial", and `record_ids` narrowing a retry to a subset --
# rewrite `delivery_attempts` and idempotency bookkeeping on stored evidence
# records, so each gets exact status/attempted/skipped assertions.


class _AlwaysFailingLiveClient:
    """Live client that never delivers, used to manufacture failed records."""

    async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
        raise LiveRegEngineDeliveryError(
            "temporary outage",
            {
                "delivery_mode": "live",
                "endpoint_host": "www.regengine.co",
                "endpoint_path": "/api/v1/webhooks/ingest",
                "idempotency_key": idempotency_key,
                "status_code": 503,
            },
        )


def _seed_failed_records(tmp_path, count, filename):
    """Generate `count` records that all failed live delivery, newest last.

    Each step gets its own idempotency key, so the records land in distinct
    retry groups -- which is what lets one group recover while another does not.
    """
    original_live_client = controller.live_client
    controller.live_client = _AlwaysFailingLiveClient()
    try:
        client.post(
            "/api/simulate/reset",
            json={
                "batch_size": 1,
                "seed": 204,
                "persist_path": str(tmp_path / filename),
                "delivery": {
                    "mode": "live",
                    "api_key": "live-api-secret",
                    "tenant_id": "live-tenant-secret",
                },
            },
        )
        for _ in range(count):
            step = client.post("/api/simulate/step")
            assert step.status_code == 200
            assert step.json()["delivery_status"] == "failed"
    finally:
        controller.live_client = original_live_client

    events = client.get("/api/events?limit=50").json()["events"]
    assert len(events) == count
    assert all(event["delivery_status"] == "failed" for event in events)
    # /api/events is newest-first; return oldest-first so record order matches
    # the sequence order the retry candidates are drawn in.
    return list(reversed(events))


def test_delivery_retry_with_none_mode_is_skipped_without_touching_records(tmp_path):
    failed_records = _seed_failed_records(tmp_path, 2, "retry-none-events.jsonl")

    response = client.post("/api/delivery/retry", json={"delivery": {"mode": "none"}})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["requested"] == 2
    assert body["retryable"] == 2
    assert body["attempted"] == 0
    assert body["posted"] == 0
    assert body["failed"] == 0
    assert body["skipped"] == 2
    assert body["delivery_mode"] == "none"
    assert body["record_ids"] == [record["record_id"] for record in failed_records]
    assert body["error"] == "Retry requires mock or live delivery mode."

    # A skipped retry must not rewrite the evidence it declined to send.
    after = list(reversed(client.get("/api/events?limit=50").json()["events"]))
    for before, current in zip(failed_records, after):
        assert current["record_id"] == before["record_id"]
        assert current["delivery_status"] == "failed"
        assert current["delivery_attempts"] == before["delivery_attempts"]
        assert current["destination_mode"] == before["destination_mode"]


def test_delivery_retry_reports_partial_when_only_one_group_recovers(tmp_path):
    failed_records = _seed_failed_records(tmp_path, 2, "retry-partial-events.jsonl")
    recovered_id, still_failing_id = (record["record_id"] for record in failed_records)

    class RecoversOnceLiveClient:
        """Accepts the first retry group and keeps failing the second."""

        def __init__(self) -> None:
            self.calls = 0

        async def ingest(self, payload, config, idempotency_key=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return LiveIngestResult(
                    response={"accepted": len(payload.events), "rejected": 0, "events": []},
                    metadata={
                        "delivery_mode": "live",
                        "endpoint_host": "www.regengine.co",
                        "idempotency_key": idempotency_key,
                        "status_code": 200,
                    },
                )
            raise LiveRegEngineDeliveryError(
                "still unavailable",
                {
                    "delivery_mode": "live",
                    "endpoint_host": "www.regengine.co",
                    "idempotency_key": idempotency_key,
                    "status_code": 503,
                },
            )

    flaky_client = RecoversOnceLiveClient()
    original_live_client = controller.live_client
    controller.live_client = flaky_client
    try:
        response = client.post(
            "/api/delivery/retry",
            json={
                "delivery": {
                    "mode": "live",
                    "api_key": "live-api-secret",
                    "tenant_id": "live-tenant-secret",
                }
            },
        )
    finally:
        controller.live_client = original_live_client

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["requested"] == 2
    assert body["retryable"] == 2
    assert body["attempted"] == 2
    assert body["posted"] == 1
    assert body["failed"] == 1
    assert body["skipped"] == 0
    assert flaky_client.calls == 2
    assert "still unavailable" in body["error"]

    by_id = {event["record_id"]: event for event in client.get("/api/events?limit=50").json()["events"]}
    assert by_id[recovered_id]["delivery_status"] == "posted"
    assert by_id[recovered_id]["error"] is None
    assert by_id[recovered_id]["last_delivery_success_at"]
    assert by_id[recovered_id]["delivery_attempts"] == 2

    assert by_id[still_failing_id]["delivery_status"] == "failed"
    assert "still unavailable" in by_id[still_failing_id]["error"]
    assert by_id[still_failing_id]["last_delivery_success_at"] is None
    assert by_id[still_failing_id]["delivery_attempts"] == 2


def test_delivery_retry_scoped_to_record_ids_only_attempts_those_records(tmp_path):
    failed_records = _seed_failed_records(tmp_path, 3, "retry-scoped-events.jsonl")
    targeted_id = failed_records[1]["record_id"]
    untouched_ids = [failed_records[0]["record_id"], failed_records[2]["record_id"]]

    response = client.post(
        "/api/delivery/retry",
        json={"record_ids": [targeted_id], "delivery": {"mode": "mock"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["requested"] == 1
    assert body["retryable"] == 1
    assert body["attempted"] == 1
    assert body["posted"] == 1
    assert body["failed"] == 0
    assert body["skipped"] == 0
    assert body["record_ids"] == [targeted_id]

    by_id = {event["record_id"]: event for event in client.get("/api/events?limit=50").json()["events"]}
    assert by_id[targeted_id]["delivery_status"] == "posted"
    assert by_id[targeted_id]["destination_mode"] == "mock"
    assert by_id[targeted_id]["delivery_attempts"] == 2
    for record_id in untouched_ids:
        assert by_id[record_id]["delivery_status"] == "failed"
        assert by_id[record_id]["destination_mode"] == "live"
        assert by_id[record_id]["delivery_attempts"] == 1

    # The two records left behind are still retryable afterwards.
    assert client.get("/api/simulate/status").json()["stats"]["delivery"]["retryable"] == 2


def test_delivery_retry_reports_unknown_record_ids_as_skipped(tmp_path):
    failed_records = _seed_failed_records(tmp_path, 1, "retry-unknown-events.jsonl")
    known_id = failed_records[0]["record_id"]

    mixed = client.post(
        "/api/delivery/retry",
        json={"record_ids": [known_id, "no-such-record"], "delivery": {"mode": "mock"}},
    )

    assert mixed.status_code == 200
    mixed_body = mixed.json()
    assert mixed_body["status"] == "posted"
    assert mixed_body["requested"] == 2
    assert mixed_body["retryable"] == 1
    assert mixed_body["attempted"] == 1
    assert mixed_body["skipped"] == 1
    assert mixed_body["record_ids"] == [known_id]

    # Nothing retryable is left, so an all-unknown selection is empty-but-skipped.
    only_unknown = client.post(
        "/api/delivery/retry",
        json={"record_ids": ["no-such-record"], "delivery": {"mode": "mock"}},
    )

    assert only_unknown.status_code == 200
    only_unknown_body = only_unknown.json()
    assert only_unknown_body["status"] == "empty"
    assert only_unknown_body["requested"] == 1
    assert only_unknown_body["retryable"] == 0
    assert only_unknown_body["attempted"] == 0
    assert only_unknown_body["skipped"] == 1
    assert only_unknown_body["record_ids"] == []

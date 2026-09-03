"""Durability and observability regressions for the event store.

Covers:
- #185 ``add_many`` must not expose records via ``recent()``/``stats()``
  before the disk write succeeds.
- #184 ``/api/healthz`` must fail when the event store cannot write.
- #182 (store-write half) store writes and failures must emit log records.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import tenancy
from app.main import app
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import EventStore


BASE_TIME = datetime(2026, 3, 9, 9, 0, tzinfo=UTC)


def make_record(lot_code: str, minutes: int = 0) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=BASE_TIME + timedelta(minutes=minutes),
            kdes={},
        ),
        parent_lot_codes=[],
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


def _break_writes(monkeypatch, store: EventStore) -> None:
    """Make every append to the store's persist path fail like a full disk."""
    real_open = type(store.persist_path).open

    def failing_open(self, mode="r", *args, **kwargs):
        if self == store.persist_path and "r" not in mode:
            raise OSError(28, "No space left on device")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(type(store.persist_path), "open", failing_open)


class TestAddManyDurability:
    def test_failed_write_leaves_no_phantom_record(self, tmp_path, monkeypatch):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record("TLC-BASELINE-000001")])
        assert store._counter == 1

        _break_writes(monkeypatch, store)
        with pytest.raises(OSError):
            store.add_many([make_record("TLC-PHANTOM-000002", minutes=5)])

        monkeypatch.undo()

        assert [record.event.traceability_lot_code for record in store.recent()] == [
            "TLC-BASELINE-000001"
        ]
        assert store._counter == 1
        assert store.stats()["total_records"] == 1

        reloaded = EventStore(persist_path=str(persist_path))
        assert [record.event.traceability_lot_code for record in reloaded.recent()] == [
            "TLC-BASELINE-000001"
        ]
        assert reloaded._counter == 1

    def test_counter_stays_usable_after_a_failed_batch(self, tmp_path, monkeypatch):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record("TLC-OK-000001")])

        _break_writes(monkeypatch, store)
        with pytest.raises(OSError):
            store.add_many([make_record("TLC-LOST-A", minutes=1), make_record("TLC-LOST-B", minutes=2)])
        monkeypatch.undo()

        stored = store.add_many([make_record("TLC-OK-000002", minutes=3)])
        assert stored[0].sequence_no == 2

        reloaded = EventStore(persist_path=str(persist_path))
        assert [record.sequence_no for record in reloaded.recent()] == [2, 1]
        assert [record.event.traceability_lot_code for record in reloaded.recent()] == [
            "TLC-OK-000002",
            "TLC-OK-000001",
        ]

    def test_partial_write_is_truncated_back(self, tmp_path):
        """A torn line must not survive: the file is restored to its pre-write size."""
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record("TLC-KEEP-000001")])
        good_size = persist_path.stat().st_size

        class TornHandle:
            def __init__(self, handle):
                self._handle = handle

            def write(self, text):
                self._handle.write(text[: len(text) // 2])
                raise OSError(28, "No space left on device")

            def __getattr__(self, name):
                return getattr(self._handle, name)

        real_open = type(persist_path).open

        def torn_open(self, mode="r", *args, **kwargs):
            handle = real_open(self, mode, *args, **kwargs)
            if self == store.persist_path and "r" not in mode:
                class _Ctx:
                    def __enter__(_self):
                        return TornHandle(handle)

                    def __exit__(_self, *exc):
                        handle.close()
                        return False

                return _Ctx()
            return handle

        original_open = type(persist_path).open
        try:
            type(persist_path).open = torn_open  # type: ignore[assignment]
            with pytest.raises(OSError):
                store.add_many([make_record("TLC-TORN-000002", minutes=4)])
        finally:
            type(persist_path).open = original_open  # type: ignore[assignment]

        assert persist_path.stat().st_size == good_size
        reloaded = EventStore(persist_path=str(persist_path))
        assert [record.event.traceability_lot_code for record in reloaded.recent()] == [
            "TLC-KEEP-000001"
        ]

    def test_empty_batch_is_a_no_op(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        assert store.add_many([]) == []
        assert store._counter == 0


class TestStoreLogging:
    def test_successful_write_logs(self, tmp_path, caplog):
        store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
        with caplog.at_level(logging.INFO, logger="app.store"):
            store.add_many([make_record("TLC-LOG-000001")])
        assert any(
            record.levelno == logging.INFO and "persisted" in record.getMessage()
            for record in caplog.records
        )

    def test_failed_write_logs_before_raising(self, tmp_path, monkeypatch, caplog):
        store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
        _break_writes(monkeypatch, store)
        with caplog.at_level(logging.ERROR, logger="app.store"):
            with pytest.raises(OSError):
                store.add_many([make_record("TLC-LOG-FAIL")])
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert errors, "expected an error log record for the failed store write"
        assert any("persist" in record.getMessage() for record in errors)
        assert any(getattr(record, "persist_path", None) == str(store.persist_path) for record in errors)


class TestWriteProbe:
    def test_probe_reports_ok_and_does_not_mutate_records(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record("TLC-PROBE-000001")])
        size_before = persist_path.stat().st_size

        probe = store.check_writable()

        assert probe["ok"] is True
        assert probe["error"] is None
        assert probe["persist_path"] == str(persist_path)
        assert persist_path.stat().st_size == size_before
        assert [record.event.traceability_lot_code for record in store.recent()] == [
            "TLC-PROBE-000001"
        ]
        assert EventStore(persist_path=str(persist_path))._counter == 1

    def test_probe_does_not_create_the_persist_file(self, tmp_path):
        persist_path = tmp_path / "nested" / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        assert store.check_writable()["ok"] is True
        assert not persist_path.exists()

    def test_probe_reports_failure(self, tmp_path, monkeypatch):
        store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
        _break_writes(monkeypatch, store)
        probe = store.check_writable()
        assert probe["ok"] is False
        assert "No space left on device" in probe["error"]


class TestHealthEndpoints:
    @pytest.fixture()
    def client(self):
        with TestClient(app) as test_client:
            yield test_client

    def test_healthz_ok_when_store_is_writable(self, client):
        response = client.get("/api/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["store"]["ok"] is True
        assert body["build"]

    def test_healthz_fails_when_store_cannot_write(self, client, monkeypatch):
        _break_writes(monkeypatch, tenancy.store)
        response = client.get("/api/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["store"]["ok"] is False
        assert "No space left on device" in body["store"]["error"]

    def test_healthz_logs_when_store_cannot_write(self, client, monkeypatch, caplog):
        _break_writes(monkeypatch, tenancy.store)
        with caplog.at_level(logging.ERROR):
            client.get("/api/healthz")
        assert any(
            "not writable" in record.getMessage()
            for record in caplog.records
            if record.name == "app.routers.health"
        )

    def test_health_reports_store_state_without_breaking_the_console(self, client, monkeypatch):
        healthy = client.get("/api/health")
        assert healthy.status_code == 200
        assert healthy.json()["ok"] is True
        assert healthy.json()["store"]["ok"] is True

        _break_writes(monkeypatch, tenancy.store)
        broken = client.get("/api/health")
        # The console polls /api/health and throws on non-2xx, so it stays 200
        # while reporting ok: false.
        assert broken.status_code == 200
        assert broken.json()["ok"] is False
        assert broken.json()["store"]["ok"] is False

    def test_health_probes_the_tenant_scoped_store(self, client, monkeypatch):
        monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "probe-user")
        monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "probe-pass")
        headers = {"Authorization": "Basic cHJvYmUtdXNlcjpwcm9iZS1wYXNz"}

        response = client.get("/api/health", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["tenant"] == "probe-user"
        assert body["ok"] is True
        tenant_store = tenancy.get_tenant_controller_for_id("probe-user").store
        assert body["store"]["persist_path"] == str(tenant_store.persist_path)
        assert body["store"]["persist_path"] != str(tenancy.store.persist_path)

        _break_writes(monkeypatch, tenant_store)
        broken = client.get("/api/health", headers=headers)
        assert broken.json()["ok"] is False
        # The default tenant's store is untouched, so healthz stays healthy.
        assert client.get("/api/healthz").status_code == 200

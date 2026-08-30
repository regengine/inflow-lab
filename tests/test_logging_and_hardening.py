"""Failures reach the log stream, and the export/delete paths are hardened.

Regression cover for #182 and #65.

#182 -- none of the core modules called `logging` at all. Delivery failures,
tenant provisioning and store write failures happened with zero log output; the
only record was whatever the HTTP caller happened to be holding. Verified in the
issue by pointing a store at `/dev/full` and capturing the root logger: the call
raised ENOSPC and emitted exactly zero log records.

#65 -- CSV formula injection in the FDA export, a recursive delete resting on an
upstream validator, and `_all_records()` reparsing the whole JSONL on every read.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import tenancy
from app.controller import SimulationController
from app.engine import LegitFlowEngine
from app.fda_export import render_fda_request_csv
from app.mock_service import MockRegEngineService
from app.regengine_client import LiveRegEngineClient
from app.scenario_saves import ScenarioSaveStore
from app.schemas.domain import CTEType, RegEngineEvent, StoredEventRecord
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import DeliveryConfig, SimulationConfig
from app.store import EventStore


def _controller(tmp_path: Path, tenant_id: str = "test-tenant") -> SimulationController:
    return SimulationController(
        engine=LegitFlowEngine(seed=204),
        store=EventStore(persist_path=str(tmp_path / "events.jsonl")),
        scenario_saves=ScenarioSaveStore(save_dir=str(tmp_path / "saves")),
        mock_service=MockRegEngineService(),
        live_client=LiveRegEngineClient(),
        tenant_id=tenant_id,
    )


def _record(**kwargs) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=kwargs.pop("lot_code", "TLC-1"),
            product_description=kwargs.pop("product_description", "Romaine"),
            quantity=10.0,
            unit_of_measure="cases",
            location_name=kwargs.pop("location_name", "Valley Farms"),
            timestamp="2026-02-10T08:00:00Z",
            kdes=kwargs.pop("kdes", {}),
        ),
    )


# --------------------------------------------------------------------------
# #182 -- logging
# --------------------------------------------------------------------------


def test_a_failed_mock_delivery_is_logged(tmp_path, caplog):
    controller = _controller(tmp_path, tenant_id="acme")
    # `invalid_key` friction makes the mock raise the same way a real 401 would.
    config = SimulationConfig(
        persist_path=str(tmp_path / "events.jsonl"),
        delivery=DeliveryConfig(mode="mock", mock_friction=["invalid_key"]),
    )
    payload = IngestPayload(source="test", events=[_record().event])

    with caplog.at_level(logging.ERROR, logger="app.controller"):
        outcome = asyncio.run(
            controller._deliver_payload(payload, config, idempotency_key="idem-123")
        )

    assert outcome.delivery_status == "failed"
    failures = [r for r in caplog.records if "Delivery failed" in r.message]
    assert len(failures) == 1, "a delivery failure left no trace in the log stream"

    line = failures[0].getMessage()
    assert "tenant=acme" in line
    assert "mode=mock" in line
    assert "idempotency_key=idem-123" in line


def test_a_successful_delivery_logs_nothing(tmp_path, caplog):
    controller = _controller(tmp_path)
    config = SimulationConfig(
        persist_path=str(tmp_path / "events.jsonl"),
        delivery=DeliveryConfig(mode="mock"),
    )
    payload = IngestPayload(source="test", events=[_record().event])

    with caplog.at_level(logging.WARNING, logger="app.controller"):
        outcome = asyncio.run(controller._deliver_payload(payload, config))

    assert outcome.delivery_status == "posted"
    assert [r for r in caplog.records if "Delivery failed" in r.message] == []


def test_tenant_provisioning_is_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", tmp_path / "tenants")

    with caplog.at_level(logging.INFO, logger="app.tenancy"):
        tenancy._create_tenant_controller("brand-new-tenant")

    provisioned = [r for r in caplog.records if "Provisioned tenant controller" in r.message]
    assert len(provisioned) == 1
    assert "brand-new-tenant" in provisioned[0].getMessage()


def test_a_store_write_failure_is_logged_before_it_propagates(tmp_path, caplog):
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    # A directory where the log file should be: every open(..., "a") fails.
    store.persist_path.unlink(missing_ok=True)
    store.persist_path.mkdir(parents=True)

    with caplog.at_level(logging.ERROR, logger="app.store"):
        with pytest.raises(OSError):
            store.add_many([_record()])

    assert [r for r in caplog.records if "EventStore append failed" in r.message]


def test_the_delivery_log_line_does_not_leak_the_api_key(tmp_path, caplog):
    controller = _controller(tmp_path)
    secret = "regengine-live-key-do-not-log"
    controller.config = controller.config.model_copy(
        update={"delivery": DeliveryConfig(mode="mock", api_key=secret)}
    )
    with caplog.at_level(logging.ERROR, logger="app.controller"):
        controller._log_delivery_failure(
            "mock",
            "idem-1",
            IngestPayload(source="test", events=[_record().event]),
            RuntimeError(f"upstream rejected key {secret}"),
        )

    assert secret not in caplog.text


# --------------------------------------------------------------------------
# #65 -- CSV formula injection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)", "\tlead", "\rlead"])
def test_formula_prefixed_cells_are_neutralised(payload):
    csv_text = render_fda_request_csv(
        [_record(product_description=payload, location_name=payload)],
        location_gln=lambda name: "",
    )

    # Read the CSV back the way a spreadsheet would, so the assertion is about
    # the parsed cell value rather than about quoting in the raw text.
    row = next(iter(csv.DictReader(io.StringIO(csv_text))))

    assert row["Product Description"] == "'" + payload
    assert row["Location Description"] == "'" + payload
    for value in row.values():
        assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), f"live formula: {value!r}"


def test_neutralising_does_not_mangle_ordinary_values():
    csv_text = render_fda_request_csv(
        [_record(product_description="Romaine Lettuce", location_name="Valley Farms")],
        location_gln=lambda name: "",
    )

    assert "Romaine Lettuce" in csv_text
    assert "'Romaine" not in csv_text


def test_negative_quantities_are_still_written_as_numbers():
    # Quantity is a float, not a string, so the neutraliser must leave it alone.
    csv_text = render_fda_request_csv([_record()], location_gln=lambda name: "")

    assert "10.0" in csv_text


# --------------------------------------------------------------------------
# #65 -- delete guard and read caching
# --------------------------------------------------------------------------


def test_delete_refuses_a_path_outside_the_tenant_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", tmp_path / "tenants")
    outside = tmp_path / "not-a-tenant"
    outside.mkdir(parents=True)

    with pytest.raises(HTTPException) as excinfo:
        tenancy.assert_within_tenant_root(outside)

    assert "outside the tenant root" in str(excinfo.value.detail)


def test_delete_refuses_the_tenant_root_itself(tmp_path, monkeypatch):
    root = tmp_path / "tenants"
    root.mkdir(parents=True)
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", root)

    with pytest.raises(HTTPException):
        tenancy.assert_within_tenant_root(root)


def test_delete_refuses_a_symlinked_tenant_directory(tmp_path, monkeypatch):
    root = tmp_path / "tenants"
    root.mkdir(parents=True)
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "sneaky").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(HTTPException):
        tenancy.assert_within_tenant_root(root / "sneaky")


def test_delete_allows_a_real_tenant_directory(tmp_path, monkeypatch):
    root = tmp_path / "tenants"
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", root)
    tenant = root / "acme"
    tenant.mkdir(parents=True)

    assert tenancy.assert_within_tenant_root(tenant) == tenant.resolve()


def test_reads_are_served_from_cache_until_the_log_changes(tmp_path):
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.add_many([_record(lot_code=f"TLC-{index}") for index in range(50)])

    calls: list[int] = []
    original = store.read_persisted_records

    def counting_read(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    store.read_persisted_records = counting_read  # type: ignore[method-assign]

    store.stats()
    store.stats()
    store.stats()
    assert len(calls) == 1, "the whole log was reparsed on every read"

    store.add_many([_record(lot_code="TLC-NEW")])
    assert store.stats()["total_records"] == 51, "a write did not invalidate the cache"


def test_reads_still_see_records_beyond_the_in_memory_ring(tmp_path):
    # The issue proposed serving reads from the deque instead. That would have
    # silently truncated every full-log read at max_records, so the cache reads
    # from disk and caches the parse rather than swapping the source.
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"), max_records=10)
    store.add_many([_record(lot_code=f"TLC-{index}") for index in range(25)])

    assert store.stats()["total_records"] == 25
    assert len(store.recent(limit=100)) == 10

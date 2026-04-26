from datetime import UTC, datetime, timedelta

from app.models import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import EventStore


BASE_TIME = datetime(2026, 2, 5, 8, 30, tzinfo=UTC)


def make_record(
    lot_code: str,
    cte_type: CTEType,
    minutes: int,
    parent_lot_codes: list[str] | None = None,
    kdes: dict | None = None,
) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=RegEngineEvent(
            cte_type=cte_type,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=BASE_TIME + timedelta(minutes=minutes),
            kdes=kdes or {},
        ),
        parent_lot_codes=parent_lot_codes or [],
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


def test_store_loads_existing_jsonl_records_on_initialization(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many(
        [
            make_record("TLC-RELOAD-000001", CTEType.HARVESTING, 0),
            make_record("TLC-RELOAD-000002", CTEType.COOLING, 10),
        ]
    )

    reloaded = EventStore(persist_path=str(persist_path))
    recent = reloaded.recent()

    assert [record.event.traceability_lot_code for record in recent] == [
        "TLC-RELOAD-000002",
        "TLC-RELOAD-000001",
    ]
    assert [record.sequence_no for record in recent] == [2, 1]

    stored = reloaded.add_many([make_record("TLC-RELOAD-000003", CTEType.SHIPPING, 20)])
    assert stored[0].sequence_no == 3


def test_store_updates_delivery_retry_metadata_on_disk(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    stored = store.add_many([make_record("TLC-RETRY-000001", CTEType.HARVESTING, 0)])
    failed_record = stored[0].model_copy(
        update={
            "delivery_status": "failed",
            "delivery_attempts": 1,
            "error": "temporary outage",
        }
    )
    store.update_many([failed_record])

    reloaded = EventStore(persist_path=str(persist_path))
    record = reloaded.recent()[0]

    assert record.record_id == stored[0].record_id
    assert record.sequence_no == 1
    assert record.delivery_status == "failed"
    assert record.delivery_attempts == 1
    assert record.error == "temporary outage"


def test_history_queries_use_persisted_records_beyond_memory_window(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path), max_records=2)
    records = [
        make_record("TLC-HISTORY-HARVEST", CTEType.HARVESTING, 0),
        make_record(
            "TLC-HISTORY-PACKED",
            CTEType.INITIAL_PACKING,
            10,
            parent_lot_codes=["TLC-HISTORY-HARVEST"],
            kdes={"source_traceability_lot_code": "TLC-HISTORY-HARVEST"},
        ),
        make_record(
            "TLC-HISTORY-TRANSFORMED",
            CTEType.TRANSFORMATION,
            20,
            parent_lot_codes=["TLC-HISTORY-PACKED"],
            kdes={"input_traceability_lot_codes": ["TLC-HISTORY-PACKED"]},
        ),
    ]
    store.add_many(records)

    assert [record.event.traceability_lot_code for record in store.recent()] == [
        "TLC-HISTORY-TRANSFORMED",
        "TLC-HISTORY-PACKED",
    ]
    assert store.stats()["total_records"] == 3
    assert [record.event.traceability_lot_code for record in store.all_between()] == [
        "TLC-HISTORY-HARVEST",
        "TLC-HISTORY-PACKED",
        "TLC-HISTORY-TRANSFORMED",
    ]
    assert [record.event.traceability_lot_code for record in store.lineage("TLC-HISTORY-TRANSFORMED")] == [
        "TLC-HISTORY-HARVEST",
        "TLC-HISTORY-PACKED",
        "TLC-HISTORY-TRANSFORMED",
    ]


def test_failed_delivery_retry_lookup_and_update_use_full_persisted_history(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path), max_records=1)
    stored = store.add_many(
        [
            make_record("TLC-OLD-FAILED", CTEType.HARVESTING, 0).model_copy(
                update={
                    "delivery_status": "failed",
                    "delivery_attempts": 1,
                    "error": "temporary outage",
                }
            ),
            make_record("TLC-NEW-GENERATED", CTEType.COOLING, 10),
        ]
    )

    failed_records = store.failed_delivery_records()
    assert [record.record_id for record in failed_records] == [stored[0].record_id]

    retried = failed_records[0].model_copy(
        update={"delivery_status": "posted", "delivery_attempts": 2, "error": None}
    )
    store.update_many([retried])

    reloaded = EventStore(persist_path=str(persist_path), max_records=1)
    records = reloaded.all_between()
    assert [record.event.traceability_lot_code for record in records] == [
        "TLC-OLD-FAILED",
        "TLC-NEW-GENERATED",
    ]
    assert records[0].delivery_status == "posted"
    assert records[0].delivery_attempts == 2
    assert records[1].delivery_status == "generated"


def test_lineage_for_transformed_output_includes_upstream_history_and_direct_query(tmp_path):
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    records = [
        make_record("TLC-HARVEST-A", CTEType.HARVESTING, 0),
        make_record("TLC-HARVEST-B", CTEType.HARVESTING, 5),
        make_record(
            "TLC-PACKED-A",
            CTEType.INITIAL_PACKING,
            10,
            parent_lot_codes=["TLC-HARVEST-A"],
            kdes={"source_traceability_lot_code": "TLC-HARVEST-A"},
        ),
        make_record(
            "TLC-PACKED-B",
            CTEType.INITIAL_PACKING,
            15,
            parent_lot_codes=["TLC-HARVEST-B"],
            kdes={"source_traceability_lot_code": "TLC-HARVEST-B"},
        ),
        make_record(
            "TLC-TRANSFORMED",
            CTEType.TRANSFORMATION,
            40,
            parent_lot_codes=["TLC-PACKED-A", "TLC-PACKED-B"],
            kdes={"input_traceability_lot_codes": ["TLC-PACKED-A", "TLC-PACKED-B"]},
        ),
    ]
    store.add_many(records)

    output_lineage = store.lineage("TLC-TRANSFORMED")
    assert [record.event.traceability_lot_code for record in output_lineage] == [
        "TLC-HARVEST-A",
        "TLC-HARVEST-B",
        "TLC-PACKED-A",
        "TLC-PACKED-B",
        "TLC-TRANSFORMED",
    ]
    assert [node.lot_code for node in store.lineage_nodes(output_lineage)] == [
        "TLC-HARVEST-A",
        "TLC-HARVEST-B",
        "TLC-PACKED-A",
        "TLC-PACKED-B",
        "TLC-TRANSFORMED",
    ]
    assert [
        (edge.source_lot_code, edge.target_lot_code, edge.cte_type.value)
        for edge in store.lineage_edges(output_lineage)
    ] == [
        ("TLC-HARVEST-A", "TLC-PACKED-A", "initial_packing"),
        ("TLC-HARVEST-B", "TLC-PACKED-B", "initial_packing"),
        ("TLC-PACKED-A", "TLC-TRANSFORMED", "transformation"),
        ("TLC-PACKED-B", "TLC-TRANSFORMED", "transformation"),
    ]

    direct_input_lineage = store.lineage("TLC-PACKED-A")
    direct_input_lots = [record.event.traceability_lot_code for record in direct_input_lineage]
    assert "TLC-PACKED-A" in direct_input_lots
    assert "TLC-TRANSFORMED" in direct_input_lots

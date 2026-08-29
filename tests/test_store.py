from datetime import UTC, datetime, timedelta

from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
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


# ---------------------------------------------------------------------------
# ``_records`` orientation — newest-first is a contract, not a convention.
#
# ``add_many`` uses ``appendleft``, ``recent()`` reads a left-slice, and
# ``maxlen`` eviction drops from the right. An inverted deque therefore does
# not merely reorder reads: the next write evicts the newest record.
#
# Every test below reads back through the SAME store instance. That is the
# whole point — the pre-existing tests reload into a fresh ``EventStore``,
# where ``_load_from_disk`` re-sorts and re-reverses, which silently repaired
# the state before anything was asserted about it.
# ---------------------------------------------------------------------------


def test_update_many_keeps_newest_first_in_the_same_instance(tmp_path):
    """A delivery retry must not flip the store's read order.

    ``update_many`` rebuilds the ring from ``_all_records()``, which sorts
    *ascending* by ``sequence_no``. Rebuilding straight from that ordering
    leaves the deque oldest-first, so ``recent()`` starts serving the oldest
    events. One retry — a single click in the UI — is enough to trigger it.
    """
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    stored = store.add_many(
        [
            make_record("TLC-ORDER-000001", CTEType.HARVESTING, 0),
            make_record("TLC-ORDER-000002", CTEType.COOLING, 10),
            make_record("TLC-ORDER-000003", CTEType.SHIPPING, 20),
        ]
    )
    before = [record.event.traceability_lot_code for record in store.recent()]
    assert before == ["TLC-ORDER-000003", "TLC-ORDER-000002", "TLC-ORDER-000001"]

    retried = stored[0].model_copy(update={"delivery_status": "posted"})
    store.update_many([retried])

    after = [record.event.traceability_lot_code for record in store.recent()]
    assert after == before, "update_many inverted the store's read order"


def test_update_many_leaves_eviction_dropping_the_oldest(tmp_path):
    """After a retry, the next write must still evict the oldest record.

    This is the second-order failure and the more damaging one: with the
    ring inverted, ``appendleft`` pushes the newest record in at the left
    while ``maxlen`` evicts from the right — which is now where the *newest*
    record lives. Writes silently delete the data just written.
    """
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"), max_records=3)
    stored = store.add_many(
        [
            make_record("TLC-EVICT-000001", CTEType.HARVESTING, 0),
            make_record("TLC-EVICT-000002", CTEType.COOLING, 10),
            make_record("TLC-EVICT-000003", CTEType.SHIPPING, 20),
        ]
    )

    store.update_many([stored[0].model_copy(update={"delivery_status": "posted"})])
    store.add_many([make_record("TLC-EVICT-000004", CTEType.RECEIVING, 30)])

    lot_codes = [record.event.traceability_lot_code for record in store.recent()]
    assert lot_codes[0] == "TLC-EVICT-000004", "the newest write was evicted by its own insert"
    assert "TLC-EVICT-000001" not in lot_codes, "eviction dropped the wrong end"
    assert lot_codes == ["TLC-EVICT-000004", "TLC-EVICT-000003", "TLC-EVICT-000002"]


def test_replace_all_over_capacity_retains_the_newest_records(tmp_path):
    """Over capacity, ``replace_all`` must keep the newest records.

    ``deque(iterable, maxlen=n)`` keeps the *last* n items, so reversing an
    oldest-first list before truncating retains the n oldest and discards
    everything recent. Truncation has to happen while the sequence is still
    oldest-first.
    """
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"), max_records=2)
    records = []
    for index in range(5):
        record = make_record(f"TLC-CAP-{index:06d}", CTEType.HARVESTING, index * 5)
        record.sequence_no = index + 1
        records.append(record)

    store.replace_all(records)

    lot_codes = [record.event.traceability_lot_code for record in store.recent()]
    assert lot_codes == ["TLC-CAP-000004", "TLC-CAP-000003"], (
        "replace_all retained the oldest records and discarded the newest"
    )


def _failed_record(lot_code: str, minutes: int) -> StoredEventRecord:
    return make_record(lot_code, CTEType.HARVESTING, minutes).model_copy(
        update={"delivery_status": "failed", "delivery_attempts": 1, "error": "temporary outage"}
    )


def test_failed_delivery_records_treats_an_explicit_empty_list_as_nothing(tmp_path):
    """#211: ``record_ids=[]`` selects nothing, not everything.

    #144 fixed this at the caller (SimulationController.retry_failed_delivery
    short-circuits an explicitly empty list before consulting the store),
    but the store's own filter stayed ``set(record_ids or [])`` -- an empty
    set, which the comprehension then read as "no filter, match every
    failed record". Any future direct caller of this method would have got
    "retry everything" from a request that said "retry nothing". Asserted
    here against the store API directly, with no controller in the way.
    """
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    stored = store.add_many(
        [
            _failed_record("TLC-EMPTY-FILTER-A", 0),
            _failed_record("TLC-EMPTY-FILTER-B", 10),
        ]
    )

    assert store.failed_delivery_records(record_ids=[]) == []

    # The other two arms of the same contract, so the fix above cannot be
    # "made to pass" by breaking either one.
    assert [record.record_id for record in store.failed_delivery_records()] == [
        record.record_id for record in stored
    ]
    assert [record.record_id for record in store.failed_delivery_records(record_ids=None)] == [
        record.record_id for record in stored
    ]
    assert [
        record.record_id
        for record in store.failed_delivery_records(record_ids=[stored[1].record_id])
    ] == [stored[1].record_id]


def test_failed_delivery_records_ignores_unknown_ids_without_widening_the_filter(tmp_path):
    """A filter naming only records that do not exist still matches nothing.

    The complement of the empty-list case: ``set(record_ids or [])`` was
    only falsy for an empty list, so this arm already behaved -- pinned so
    a future "simplification" back to a truthiness check is caught by more
    than one test.
    """
    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.add_many([_failed_record("TLC-UNKNOWN-FILTER", 0)])

    assert store.failed_delivery_records(record_ids=["no-such-record-id"]) == []

"""Regression tests for two persisted-record write-path bugs in ``app.store``.

- #185: ``add_many`` used to append to the in-memory deque and bump
  ``_counter`` *before* the disk write succeeded, so a failed write left a
  phantom record visible via ``recent()`` that silently vanished on the next
  reload. These tests assert a failed write leaves no such phantom.

- #90: ``add_many`` scrubbed secret-named fields before writing, but
  ``update_many`` and ``replace_all`` rewrote the whole file with no scrub.
  These tests assert all three write paths mask secret-named fields on disk,
  not just the append path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.store as store_module
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import MASKED_SECRET, EventStore

BASE_TIME = datetime(2026, 2, 5, 8, 30, tzinfo=UTC)


def make_record(lot_code: str, kdes: dict | None = None) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=BASE_TIME,
            kdes=kdes or {},
        ),
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


# ---------------------------------------------------------------------------
# #185 — add_many must not expose a record that never reached disk.
# ---------------------------------------------------------------------------


def test_add_many_does_not_expose_a_phantom_record_when_the_disk_write_fails(tmp_path):
    """Reproduces the issue's own verified evidence almost verbatim: a
    baseline record lands on a real file, then persist_path is pointed at
    /dev/full (always raises ENOSPC on write) for a second call.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-BASELINE-000001")])

    assert [record.event.traceability_lot_code for record in store.recent()] == ["TLC-BASELINE-000001"]
    assert store._counter == 1

    store.persist_path = Path("/dev/full")
    with pytest.raises(OSError):
        store.add_many([make_record("TLC-PHANTOM-000002")])

    # No phantom: the failed record must not be visible, and the counter
    # must not have advanced past what actually reached disk.
    assert [record.event.traceability_lot_code for record in store.recent()] == ["TLC-BASELINE-000001"]
    assert store._counter == 1

    # A follow-up call must continue the sequence at 2, not skip to 3 --
    # proving the counter genuinely wasn't bumped, not just that recent()
    # happens to hide it.
    store.persist_path = persist_path
    resumed = store.add_many([make_record("TLC-RESUMED-000002")])
    assert resumed[0].sequence_no == 2

    # And reloading from the real file (what a restart or .configure() does)
    # must agree: the phantom was never written, so it can't reappear either.
    store._load_from_disk()
    assert [record.event.traceability_lot_code for record in store.recent()] == [
        "TLC-RESUMED-000002",
        "TLC-BASELINE-000001",
    ]
    assert store._counter == 2


def test_add_many_leaves_no_phantom_when_a_later_write_in_the_same_batch_fails(tmp_path, monkeypatch):
    """A true mid-batch failure within a single add_many() call: the first
    record's line reaches disk, the second record's write() raises. Only
    the first record's commit to memory may stand.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    real_open = Path.open
    writes = {"count": 0}

    def flaky_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self != store.persist_path:
            return handle

        real_write = handle.write

        def flaky_write(data):
            writes["count"] += 1
            if writes["count"] == 2:
                raise OSError("simulated mid-batch disk failure")
            return real_write(data)

        handle.write = flaky_write
        return handle

    monkeypatch.setattr(Path, "open", flaky_open)

    with pytest.raises(OSError):
        store.add_many([make_record("TLC-OK-000001"), make_record("TLC-FAILS-000002")])

    lot_codes = [record.event.traceability_lot_code for record in store.recent()]
    assert lot_codes == ["TLC-OK-000001"], "the failed record must not be visible in memory"
    assert store._counter == 1, "the counter must not advance past what reached disk"

    # The exception must propagate, not be swallowed -- pytest.raises above
    # already proves that, and the on-disk file must match memory exactly.
    on_disk_lot_codes = [
        json.loads(line)["event"]["traceability_lot_code"]
        for line in persist_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert on_disk_lot_codes == ["TLC-OK-000001"]


# ---------------------------------------------------------------------------
# #90 — every persisted-record write path must apply the secret scrub.
# ---------------------------------------------------------------------------


def test_add_many_scrubs_secret_named_fields_on_append(tmp_path):
    """Baseline: the append path already did this. Kept here so all three
    write paths are exercised side by side in one file.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-APPEND-000001", kdes={"api_key": "sk-live-should-never-hit-disk"})])

    raw_line = persist_path.read_text(encoding="utf-8").strip()
    assert "sk-live-should-never-hit-disk" not in raw_line
    assert json.loads(raw_line)["event"]["kdes"]["api_key"] == MASKED_SECRET


def test_update_many_scrubs_secret_named_fields_on_retry_rewrite(tmp_path):
    """A KDE literally named ``api_key`` (e.g. from a CSV import) must stay
    masked after a delivery-retry rewrite, not just on the original append.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    stored = store.add_many([make_record("TLC-RETRY-000001")])

    tainted_event = stored[0].event.model_copy(
        update={"kdes": {"api_key": "sk-live-should-never-hit-disk"}}
    )
    retried = stored[0].model_copy(update={"delivery_status": "posted", "event": tainted_event})
    store.update_many([retried])

    raw_lines = [line for line in persist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(raw_lines) == 1
    assert "sk-live-should-never-hit-disk" not in raw_lines[0]
    on_disk = json.loads(raw_lines[0])
    assert on_disk["event"]["kdes"]["api_key"] == MASKED_SECRET
    assert on_disk["delivery_status"] == "posted"

    # A restart (fresh instance reloading the same file) must only ever be
    # able to see the masked value -- the secret never round-trips.
    reloaded = EventStore(persist_path=str(persist_path))
    assert reloaded.recent()[0].event.kdes["api_key"] == MASKED_SECRET


def test_replace_all_scrubs_secret_named_fields_on_scenario_load(tmp_path):
    """A scenario-load rewrite (``replace_all``) must mask secret-named
    fields exactly like the append path does.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    tainted = make_record("TLC-SCENARIO-000001", kdes={"api_key": "sk-scenario-should-never-hit-disk"})
    tainted.sequence_no = 1
    store.replace_all([tainted])

    raw_lines = [line for line in persist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(raw_lines) == 1
    assert "sk-scenario-should-never-hit-disk" not in raw_lines[0]
    on_disk = json.loads(raw_lines[0])
    assert on_disk["event"]["kdes"]["api_key"] == MASKED_SECRET

    reloaded = EventStore(persist_path=str(persist_path))
    assert reloaded.recent()[0].event.kdes["api_key"] == MASKED_SECRET


def test_all_three_write_paths_route_through_the_shared_serializer(tmp_path, monkeypatch):
    """#90's actual ask: one serialization helper behind all three write
    paths, so the scrub can't drift out of sync again. Confirms the
    structural fix, not just its externally observable effect.
    """
    calls: list[str] = []
    real_serialize = store_module._serialize_record

    def counting_serialize(record):
        calls.append(record.record_id)
        return real_serialize(record)

    monkeypatch.setattr(store_module, "_serialize_record", counting_serialize)

    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))

    stored = store.add_many([make_record("TLC-A-000001"), make_record("TLC-B-000002")])
    assert len(calls) == 2
    calls.clear()

    store.update_many([stored[0].model_copy(update={"delivery_status": "posted"})])
    assert len(calls) == 2  # rewrites both records in the file
    calls.clear()

    store.replace_all(stored)
    assert len(calls) == 2


def test_update_many_does_not_publish_a_rewrite_that_failed_to_persist(tmp_path, monkeypatch):
    """A failed rewrite must leave memory matching disk, not a half-applied update.

    Same guarantee add_many gives: update_many persists the whole file before
    it publishes the new records to the in-memory deque, so a rewrite that
    raises leaves recent() reporting exactly what is still on disk.
    """
    from app import store as store_mod

    store = EventStore(persist_path=tmp_path / "events.jsonl")
    (stored,) = store.add_many([make_record(lot_code="LOT-ORIGINAL")])

    retried = stored.model_copy(deep=True)
    retried.event.traceability_lot_code = "LOT-RETRIED"

    def explode(_record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store_mod, "_serialize_record", explode)
    with pytest.raises(OSError):
        store.update_many([retried])
    monkeypatch.undo()

    assert [r.event.traceability_lot_code for r in store.recent()] == ["LOT-ORIGINAL"]
    on_disk = [json.loads(line) for line in
               (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["event"]["traceability_lot_code"] for r in on_disk] == ["LOT-ORIGINAL"]

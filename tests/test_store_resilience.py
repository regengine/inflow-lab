"""Tests for #93: EventStore must not abort a whole read on one malformed
JSONL line.

Before this fix, ``EventStore.read_persisted_records`` raised on the first
line that failed to parse -- a JSON-syntax error, a schema mismatch, a
write torn by a crash mid-append -- which took out every reader for that
tenant (status, lineage, stats, exports, ``/api/health`` all funnel through
``_all_records()`` -> ``read_persisted_records()``) and, because the
default store is built at import time (app/tenancy.py, not owned by this
agent), could stop the app from starting at all on a corrupt persistent
volume.

This file covers what app/store.py alone can guarantee:

- a store file containing a malformed line (both a syntactically-broken
  one and a truncated final line) still serves every readable record;
- constructing/reconfiguring a store on a corrupt file never raises --
  the layer directly under app/tenancy.py's import-time construction;
- the corruption is surfaced (logged, path and line number, never the raw
  line itself) rather than silently swallowed;
- a fully-valid store is completely unaffected, including no spurious log
  noise;
- how a caller tells a partial read from a complete one:
  ``EventStore.last_read_was_complete`` / ``last_read_corrupt_lines``, and
  the same summary folded into ``stats()["store_integrity"]`` for callers
  that only ever see the dict ``/api/simulate/status`` and ``/api/health``
  already return (both flow ``stats()`` into their response, and
  ``stats: dict[str, Any]`` in ``StatusResponse`` has no fixed schema, so
  the new key needs no change anywhere else to reach them).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import CorruptRecordLine, EventStore

BASE_TIME = datetime(2026, 2, 5, 8, 30, tzinfo=UTC)


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
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


def _append_raw(path: Path, raw_text: str) -> None:
    """Append text directly to the JSONL file, bypassing EventStore.

    No implicit trailing newline is added -- the caller decides whether the
    appended line is newline-terminated (an ordinary bad line) or not (a
    write torn before its newline ever landed, #93's "truncated final
    line").
    """
    with path.open("a", encoding="utf-8") as handle:
        handle.write(raw_text)


# ---------------------------------------------------------------------------
# Core fix: one bad line must not take out the read, whatever shape the
# corruption takes.
# ---------------------------------------------------------------------------


def test_a_malformed_line_is_skipped_and_the_good_records_around_it_still_load(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-BEFORE"), make_record("TLC-AFTER", minutes=5)])

    # Sandwich a syntactically-broken line between the two good ones.
    good_lines = persist_path.read_text(encoding="utf-8").splitlines()
    persist_path.write_text(
        good_lines[0] + "\n" + "{not valid json at all\n" + good_lines[1] + "\n",
        encoding="utf-8",
    )

    reloaded = EventStore(persist_path=str(persist_path))  # must not raise

    lot_codes = [record.event.traceability_lot_code for record in reloaded.recent()]
    assert lot_codes == ["TLC-AFTER", "TLC-BEFORE"]
    assert reloaded.stats()["total_records"] == 2


def test_a_schema_invalid_line_is_skipped_the_same_way_as_a_syntactically_broken_one(tmp_path):
    """Valid JSON that doesn't validate as a StoredEventRecord -- the
    issue's own "future StoredEventRecord/CTEType change" example -- must
    be skipped and logged too, not just a bare JSON-syntax error.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-VALID")])
    _append_raw(persist_path, '{"record_id": "not-a-real-record"}\n')

    reloaded = EventStore(persist_path=str(persist_path))  # must not raise

    assert [r.event.traceability_lot_code for r in reloaded.recent()] == ["TLC-VALID"]
    assert reloaded.last_read_was_complete is False


def test_a_truncated_final_line_is_skipped_and_the_lines_before_it_still_load(tmp_path):
    """The acceptance criteria's other named case: a write torn before its
    newline ever landed (a crash mid-append), not a bad line with a clean
    terminator.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-COMPLETE-ONE"), make_record("TLC-COMPLETE-TWO", minutes=5)])
    # No closing brace and no trailing newline -- exactly what a process
    # killed mid-write would leave as the file's last line.
    _append_raw(persist_path, '{"record_id": "torn-mid-writ')

    reloaded = EventStore(persist_path=str(persist_path))  # must not raise

    lot_codes = {record.event.traceability_lot_code for record in reloaded.recent()}
    assert lot_codes == {"TLC-COMPLETE-ONE", "TLC-COMPLETE-TWO"}
    assert reloaded.last_read_was_complete is False
    assert len(reloaded.last_read_corrupt_lines) == 1
    assert isinstance(reloaded.last_read_corrupt_lines[0], CorruptRecordLine)
    assert reloaded.last_read_corrupt_lines[0].line_number == 3


def test_multiple_malformed_lines_are_all_skipped_not_just_the_first(tmp_path):
    """The bug was aborting on the *first* bad line. Two bad lines must
    both be skipped and both individually reported, proving the fix keeps
    going past a failure instead of just moving where it stops.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONLY-GOOD-RECORD")])

    good_line = persist_path.read_text(encoding="utf-8")
    persist_path.write_text("not json\n" + good_line + "also not json\n", encoding="utf-8")

    reloaded = EventStore(persist_path=str(persist_path))

    assert [r.event.traceability_lot_code for r in reloaded.recent()] == ["TLC-ONLY-GOOD-RECORD"]
    assert [line.line_number for line in reloaded.last_read_corrupt_lines] == [1, 3]


def test_read_persisted_records_still_returns_a_plain_list_when_corruption_is_present(tmp_path):
    """External callers depend on the return value staying exactly a plain
    list of records: mock_service.py's ``_resume_chain_hash`` does
    ``sorted(store.read_persisted_records(), ...)`` directly, and
    controller.py's ``_store_read_persisted_records`` hands this straight
    back to its own caller through ``asyncio.to_thread``. The completeness
    signal lives on the instance (``last_read_was_complete``), not on the
    return value's shape, specifically so neither of those breaks.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONE")])
    _append_raw(persist_path, "bad\n")

    result = store.read_persisted_records()

    assert isinstance(result, list)
    assert all(isinstance(item, StoredEventRecord) for item in result)
    assert len(result) == 1


def test_all_between_still_serves_the_good_records_from_a_store_with_a_corrupt_line(tmp_path):
    """The same guarantee, exercised through the public accessor
    controller.status()/exports actually call, not just read_persisted_
    records()/recent() directly.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONE"), make_record("TLC-TWO", minutes=5)])
    _append_raw(persist_path, "not json\n")

    reloaded = EventStore(persist_path=str(persist_path))

    lot_codes = {record.event.traceability_lot_code for record in reloaded.all_between()}
    assert lot_codes == {"TLC-ONE", "TLC-TWO"}


# ---------------------------------------------------------------------------
# Startup safety: construction/reconfiguration must never raise on a
# corrupt file -- the layer directly under app/tenancy.py's import-time
# construction (#93's crash-loop leg).
# ---------------------------------------------------------------------------


def test_constructing_a_store_on_a_corrupt_file_does_not_raise(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    persist_path.write_text("definitely not json\n", encoding="utf-8")

    store = EventStore(persist_path=str(persist_path))  # must not raise

    assert store.recent() == []
    assert store.last_read_was_complete is False


def test_configure_onto_a_corrupt_file_does_not_raise(tmp_path):
    """configure() re-runs _load_from_disk on the new path -- the same
    reentrant call chain __init__ uses -- so it must be equally safe on an
    already-running store, not just at first construction.
    """
    good_path = tmp_path / "good.jsonl"
    store = EventStore(persist_path=str(good_path))
    store.add_many([make_record("TLC-BEFORE-SWITCH")])

    corrupt_path = tmp_path / "corrupt.jsonl"
    corrupt_path.write_text("also not json\n", encoding="utf-8")

    store.configure(str(corrupt_path))  # must not raise

    assert store.recent() == []
    assert store.last_read_was_complete is False


# ---------------------------------------------------------------------------
# Visible, not silent: skipping a line must still surface the corruption.
# ---------------------------------------------------------------------------


def test_a_skipped_line_is_logged_with_the_path_and_line_number(tmp_path, caplog):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-GOOD")])
    _append_raw(persist_path, "garbage-line-two\n")

    caplog.set_level(logging.ERROR, logger="inflow_lab")
    store._load_from_disk()  # deterministic re-trigger of the parse pass

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    assert any(f"{persist_path}:2" in message for message in messages)


def test_the_raw_malformed_line_content_is_never_logged(tmp_path, caplog):
    """The line failed to parse *as* a record, so it could still hold a
    secret-named KDE the model never got a chance to scrub -- the same
    concern _serialize_record's scrub exists for on the write side (#90).
    The log must identify *where* the bad line is, never echo it.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    marker = "sk-should-never-appear-in-a-log-UNIQUE-MARKER-000000000000"
    _append_raw(persist_path, f"not even json but it mentions {marker}\n")

    caplog.set_level(logging.ERROR, logger="inflow_lab")
    store._load_from_disk()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in log_text
    # Sanity: this passes because the marker was withheld, not because
    # nothing was logged at all.
    assert "skipping unreadable event record" in log_text


# ---------------------------------------------------------------------------
# The completeness signal: how a caller tells a partial read from a
# complete one. #93's own framing: "callers must not mistake a truncated
# history for a complete one."
# ---------------------------------------------------------------------------


def test_a_fully_valid_store_reports_a_complete_read_with_no_error_logs(tmp_path, caplog):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONE"), make_record("TLC-TWO", minutes=5)])

    caplog.set_level(logging.ERROR, logger="inflow_lab")
    reloaded = EventStore(persist_path=str(persist_path))

    assert reloaded.last_read_was_complete is True
    assert reloaded.last_read_corrupt_lines == []
    assert reloaded.stats()["store_integrity"] == {"complete": True, "corrupt_lines_skipped": 0}
    assert not any(record.name == "inflow_lab" for record in caplog.records)


def test_stats_reports_an_incomplete_read_and_the_skipped_line_count(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONE")])
    _append_raw(persist_path, "bad line one\n")
    _append_raw(persist_path, "bad line two\n")

    reloaded = EventStore(persist_path=str(persist_path))
    stats = reloaded.stats()

    # total_records only ever counts what actually parsed -- the two bad
    # lines are absent, not counted in as zero-content records.
    assert stats["total_records"] == 1
    assert stats["store_integrity"] == {"complete": False, "corrupt_lines_skipped": 2}


def test_reset_clears_a_stale_incomplete_read_signal(tmp_path):
    persist_path = tmp_path / "events.jsonl"
    persist_path.write_text("not json\n", encoding="utf-8")
    store = EventStore(persist_path=str(persist_path))
    assert store.last_read_was_complete is False  # sanity: corruption seen on load

    store.reset()

    assert store.last_read_was_complete is True
    assert store.last_read_corrupt_lines == []


def test_blank_lines_are_still_skipped_silently_not_reported_as_corrupt(tmp_path):
    """app/routers/health.py's write probe appends a single blank line on
    every /healthz poll and depends on read_persisted_records() treating
    that as a no-op, not a corrupt line (see that module's own docstring).
    This fix must not turn that pre-existing, load-bearing behavior into a
    logged/flagged corruption.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONE")])
    _append_raw(persist_path, "\n\n")  # exactly what the health probe writes

    reloaded = EventStore(persist_path=str(persist_path))

    assert reloaded.last_read_was_complete is True
    assert reloaded.last_read_corrupt_lines == []


def test_the_sequence_counter_reflects_only_the_readable_records_after_a_torn_final_line(tmp_path):
    """The counter must resume from the last *good* record, not credit a
    corrupt/torn line as if it had reached disk successfully.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record("TLC-ONE"), make_record("TLC-TWO", minutes=5)])
    _append_raw(persist_path, '{"record_id": "torn')  # crash mid-write of a third record

    reloaded = EventStore(persist_path=str(persist_path))

    assert reloaded._counter == 2
    assert reloaded.last_read_was_complete is False

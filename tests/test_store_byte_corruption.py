"""The two halves of #93 that the skip-and-log pass left open.

``tests/test_store_resilience.py`` already covers #93's first half: a
malformed *line* -- broken JSON, a schema mismatch, a truncated final line
-- is skipped and logged rather than aborting the read. Two things it does
not cover, and could not, remained:

1. **Invalid UTF-8 bytes still aborted the whole read.** The guard there
   catches ``ValueError`` around ``model_validate_json``, but the read
   opened the file in *text* mode, so decoding happened in the ``for``
   statement itself -- one step outside that try. A run of undecodable
   bytes anywhere in the file therefore raised ``UnicodeDecodeError``
   straight past every skip-and-log branch, taking out every reader for
   that tenant and, because ``app/tenancy.py`` builds the default store at
   import time, stopping the app from starting on a corrupt volume. That
   is the same failure #93 is filed about, reached through byte corruption
   rather than a syntax error, and it is exactly what a torn write or a
   truncated volume restore produces.

2. **The append path was not durable.** ``update_many``/``replace_all``
   write via tmp + ``os.replace``; ``add_many`` only ``flush()``ed, which
   reaches the OS page cache but not stable storage, so an acknowledged
   append could still be missing after a power loss. #93 names ``fsync``
   as the fix for the append path specifically -- a tmp + rename here
   would mean rewriting the whole log on every append.

Kept in its own file rather than added to ``test_store_resilience.py`` or
``test_store_durability.py``: parallel workstreams own those, and both
halves below belong to the same issue.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import EventStore

BASE_TIME = datetime(2026, 4, 9, 7, 15, tzinfo=UTC)

# A byte sequence that is not valid UTF-8 under any interpretation: 0xff and
# 0xfe never appear in a well-formed UTF-8 stream.
INVALID_UTF8 = b"\xff\xfe\xfd"


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


def _seeded_store(tmp_path: Path, lot_codes: list[str]) -> tuple[EventStore, Path]:
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    store.add_many([make_record(lot, minutes=index) for index, lot in enumerate(lot_codes)])
    return store, persist_path


# ---------------------------------------------------------------------------
# Half 1 -- invalid UTF-8 bytes must be skipped like any other bad line.
# ---------------------------------------------------------------------------


def test_a_line_of_invalid_utf8_bytes_is_skipped_and_the_records_around_it_still_load(tmp_path):
    """The core of this half, in the same shape as the malformed-line test.

    Verified to fail before the fix with
    ``UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff`` raised out
    of ``EventStore.__init__`` -- i.e. it did not merely lose the good
    records, it could not even construct the store.
    """
    _, persist_path = _seeded_store(tmp_path, ["TLC-BEFORE", "TLC-AFTER"])
    good_lines = persist_path.read_bytes().splitlines()

    persist_path.write_bytes(
        good_lines[0] + b"\n" + b'{"payload_source": "' + INVALID_UTF8 + b'"}\n' + good_lines[1] + b"\n"
    )

    reloaded = EventStore(persist_path=str(persist_path))  # must not raise

    lot_codes = [record.event.traceability_lot_code for record in reloaded.recent()]
    assert lot_codes == ["TLC-AFTER", "TLC-BEFORE"]
    assert reloaded.stats()["total_records"] == 2


def test_a_wholly_undecodable_line_is_skipped_too(tmp_path):
    """Not just a bad string *inside* otherwise-valid JSON: a line that is
    nothing but raw binary -- what a torn write or a truncated volume
    restore actually leaves behind -- is skipped the same way.
    """
    _, persist_path = _seeded_store(tmp_path, ["TLC-KEEP-1", "TLC-KEEP-2"])
    good_lines = persist_path.read_bytes().splitlines()

    persist_path.write_bytes(
        good_lines[0] + b"\n" + INVALID_UTF8 * 8 + b"\n" + good_lines[1] + b"\n"
    )

    reloaded = EventStore(persist_path=str(persist_path))

    assert sorted(record.event.traceability_lot_code for record in reloaded.recent()) == [
        "TLC-KEEP-1",
        "TLC-KEEP-2",
    ]


def test_invalid_utf8_in_the_final_line_does_not_hide_the_records_before_it(tmp_path):
    """The torn-append shape: bytes landed, then the write died mid-record.

    The corruption is at the tail, which is where a partially-written
    append leaves it, and there is no trailing newline -- so this also
    exercises the un-terminated final line.
    """
    _, persist_path = _seeded_store(tmp_path, ["TLC-INTACT-1", "TLC-INTACT-2"])
    with persist_path.open("ab") as handle:
        handle.write(b'{"payload_source": "test-suite", "event": ' + INVALID_UTF8)

    reloaded = EventStore(persist_path=str(persist_path))

    assert len(reloaded.recent()) == 2
    assert reloaded.last_read_was_complete is False


def test_byte_corruption_is_reported_through_the_same_integrity_signal(tmp_path):
    """Skipping must stay *visible*: a read that dropped undecodable bytes
    reports itself through the same ``last_read_corrupt_lines`` /
    ``stats()["store_integrity"]`` channel a malformed line already does,
    so byte corruption is not quietly indistinguishable from a short but
    healthy store.
    """
    store, persist_path = _seeded_store(tmp_path, ["TLC-SIGNAL"])
    good_line = persist_path.read_bytes().splitlines()[0]
    persist_path.write_bytes(good_line + b"\n" + INVALID_UTF8 + b"\n")

    reloaded = EventStore(persist_path=str(persist_path))

    assert reloaded.last_read_was_complete is False
    assert [line.line_number for line in reloaded.last_read_corrupt_lines] == [2]
    integrity = reloaded.stats()["store_integrity"]
    assert integrity["complete"] is False
    assert integrity["corrupt_lines_skipped"] == 1
    assert store.persist_path == reloaded.persist_path


def test_the_undecodable_bytes_are_never_echoed_into_the_log(tmp_path, caplog):
    """Same rule the malformed-line path already follows: the line failed to
    parse *as a record*, so it could still hold a secret-named KDE the
    model never got a chance to scrub. The log gets the path and line
    number, never the content.
    """
    _, persist_path = _seeded_store(tmp_path, ["TLC-LOGGED"])
    good_line = persist_path.read_bytes().splitlines()[0]
    secret_bytes = b'{"api_key": "super-secret-value-' + INVALID_UTF8 + b'"}'
    persist_path.write_bytes(good_line + b"\n" + secret_bytes + b"\n")

    with caplog.at_level(logging.ERROR, logger="inflow_lab"):
        EventStore(persist_path=str(persist_path))

    messages = [record.getMessage() for record in caplog.records]
    assert any("skipping unreadable event record" in message for message in messages)
    assert any(f"{persist_path}:2" in message for message in messages)
    assert not any("super-secret-value" in message for message in messages)


def test_a_store_of_pure_binary_still_constructs_and_serves_an_empty_history(tmp_path):
    """The startup-safety leg of #93, at its worst.

    ``app/tenancy.py`` builds the default store at import time, so a raise
    here is a failed application start -- and with Railway's ON_FAILURE
    restart policy, a crash loop. Even a file with nothing readable in it
    must yield a degraded, diagnosable store rather than an exception.
    """
    persist_path = tmp_path / "events.jsonl"
    persist_path.write_bytes(INVALID_UTF8 * 64)

    store = EventStore(persist_path=str(persist_path))  # must not raise

    assert store.recent() == []
    assert store.stats()["total_records"] == 0
    assert store.last_read_was_complete is False


def test_a_reconfigure_onto_byte_corrupted_data_does_not_raise(tmp_path):
    """``configure()`` reloads from the new path, so it is the second way a
    corrupt file reaches the read path -- via start()/reset() repointing a
    live store rather than via construction.
    """
    healthy_path = tmp_path / "healthy.jsonl"
    store = EventStore(persist_path=str(healthy_path))
    store.add_many([make_record("TLC-HEALTHY")])

    corrupt_path = tmp_path / "corrupt.jsonl"
    corrupt_path.write_bytes(INVALID_UTF8 * 16 + b"\n")

    store.configure(str(corrupt_path))  # must not raise

    assert store.recent() == []
    assert store.last_read_was_complete is False


def test_a_healthy_store_is_completely_unaffected_by_the_binary_read(tmp_path, caplog):
    """The read now decodes through pydantic instead of the file handle, so
    pin that an ordinary, fully-valid store round-trips byte-for-byte --
    including non-ASCII content, which is precisely what a naive
    bytes-based read would be most likely to mangle.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    record = make_record("TLC-ÜNICODE-Ω")
    record.event.location_name = "Ferme Vallée Fraîche"
    store.add_many([record])

    with caplog.at_level(logging.ERROR, logger="inflow_lab"):
        reloaded = EventStore(persist_path=str(persist_path))

    assert reloaded.last_read_was_complete is True
    assert caplog.records == []
    (loaded,) = reloaded.recent()
    assert loaded.event.traceability_lot_code == "TLC-ÜNICODE-Ω"
    assert loaded.event.location_name == "Ferme Vallée Fraîche"


# ---------------------------------------------------------------------------
# Half 2 -- the append path must reach stable storage, not just the page cache.
# ---------------------------------------------------------------------------


class FsyncSpy:
    """Records every fd handed to ``os.fsync`` and calls the real one."""

    def __init__(self, real_fsync) -> None:
        self._real_fsync = real_fsync
        self.fds: list[int] = []

    def __call__(self, fd: int) -> None:
        self.fds.append(fd)
        self._real_fsync(fd)


def test_add_many_fsyncs_the_appended_batch_to_stable_storage(tmp_path, monkeypatch):
    """The durability half of #93.

    Verified to fail before the fix: ``add_many`` only flushed, so
    ``os.fsync`` was never called at all and ``spy.fds`` came back empty.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    spy = FsyncSpy(os.fsync)
    monkeypatch.setattr(os, "fsync", spy)
    store.add_many([make_record("TLC-DURABLE-1"), make_record("TLC-DURABLE-2", minutes=1)])

    assert spy.fds, "add_many returned without forcing the append to stable storage"
    # The records really are on disk afterwards, read back through a
    # completely independent store.
    reloaded = EventStore(persist_path=str(persist_path))
    assert sorted(record.event.traceability_lot_code for record in reloaded.recent()) == [
        "TLC-DURABLE-1",
        "TLC-DURABLE-2",
    ]


def test_the_batch_is_fsynced_once_not_once_per_record(tmp_path, monkeypatch):
    """Per-record ``flush()`` already covers error detection and the
    write-then-commit ordering ``_append_locked`` depends on; an fsync per
    record would make a multi-thousand-row CSV import pay a disk
    round-trip per row for no additional guarantee.
    """
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    spy = FsyncSpy(os.fsync)
    monkeypatch.setattr(os, "fsync", spy)
    store.add_many([make_record(f"TLC-BATCH-{index}", minutes=index) for index in range(25)])

    assert len(spy.fds) == 1, f"expected one fsync for the whole batch, got {len(spy.fds)}"


def test_an_empty_append_does_no_fsync_at_all(tmp_path, monkeypatch):
    """Nothing was written, so there is nothing to force to disk."""
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    spy = FsyncSpy(os.fsync)
    monkeypatch.setattr(os, "fsync", spy)
    assert store.add_many([]) == []

    assert spy.fds == []


def test_appending_to_a_character_device_still_skips_the_fsync(tmp_path, monkeypatch):
    """``persist_path`` can legitimately point at something that is not a
    regular file -- the existing durability suite uses ``/dev/full`` to
    force ENOSPC -- and such a target has no durable state to flush. Same
    ``is_file()`` constraint ``_truncate_torn_tail`` and
    ``_resync_counter_from_disk`` already carry; without it the fsync guard
    would turn that suite's deliberate ENOSPC into a different, misleading
    error.
    """
    if not Path("/dev/full").exists():  # pragma: no cover - platform guard
        return

    store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
    store.persist_path = Path("/dev/full")

    spy = FsyncSpy(os.fsync)
    monkeypatch.setattr(os, "fsync", spy)
    try:
        store.add_many([make_record("TLC-DEV-FULL")])
    except OSError:
        pass  # ENOSPC from the flush is the expected outcome here.

    assert spy.fds == [], "fsync must not be attempted on a non-regular persist path"


def test_a_corrupt_line_never_puts_its_own_content_in_the_log_or_the_record(tmp_path, caplog) -> None:
    """The no-content promise this module makes twice must actually hold.

    `read_persisted_records`'s skip-and-log branch and `CorruptRecordLine`'s
    own docstring both state that the offending line is never carried out of
    here: it failed to parse *as* a record, so it never went through
    `_scrub_secrets` and can still hold a plaintext `api_key`.

    `str(ValidationError)` broke that promise, because pydantic renders the
    rejected input into its own message as `input_value=...`. A JSONL line
    carrying a live key therefore put that key straight into the log and into
    `CorruptRecordLine.error`, which is exactly the population the promise
    exists for.

    Asserted on the ABSENCE OF `input_value` rather than only on the secret
    string, deliberately. Pydantic truncates that repr with an ellipsis, and
    where the ellipsis lands depends on the surrounding keys -- so a
    substring check for the secret can pass while a recognisable fragment of
    it is still in the log. "The rendering echoes no input at all" is the
    property that actually holds, and it does not depend on how one payload
    happens to truncate. The secret checks stay as the concrete case.
    """
    secret = "rge_live_MUST_NOT_APPEAR"
    path = tmp_path / "events.jsonl"
    # api_key first, so it lands before pydantic's truncation point: with it
    # last, the ellipsis splits the value and the exact-substring assertion
    # below would pass on a leak.
    path.write_text(json.dumps({"api_key": secret, "not": "a record"}) + "\n", encoding="utf-8")

    store = EventStore(persist_path=str(path))
    with caplog.at_level(logging.ERROR, logger="inflow_lab"):
        store.read_persisted_records()

    assert store.last_read_corrupt_lines, "the malformed line should have been skipped and recorded"
    recorded = store.last_read_corrupt_lines[0]

    # The property: nothing from the line is echoed back.
    assert "input_value" not in recorded.error, recorded.error
    assert "input_value" not in caplog.text
    # The concrete case that property exists for.
    assert secret not in recorded.error, recorded.error
    assert secret not in caplog.text
    assert "rge_live" not in recorded.error, recorded.error

    # Still diagnostic: the operator learns where the bad line is and why it
    # was rejected, just not what was in it.
    assert recorded.line_number == 1
    assert str(path) == recorded.path
    assert recorded.error, "a skipped line must still say why it was skipped"

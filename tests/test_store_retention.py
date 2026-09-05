"""#217 -- the live event log is bounded, not append-forever.

Every history read in ``app.store`` (``_all_records()``: stats(), lineage(),
all_between(), both exports, and the /api/simulate/status poll the console
runs on a timer) parses the WHOLE live JSONL log through Pydantic. With no
retention bound, every one of those gets slower for the life of a deployed
volume. ``EventStore.max_history`` caps the live log: once it exceeds the
bound by a slack of ``max(1, max_history // 10)`` records, the oldest
overflow is appended to ``<persist>.1`` beside it and the live log is
rewritten to the newest ``max_history`` records through the same atomic-swap
path ``update_many`` uses.

What is pinned here: the trim happens once the slack is crossed and not per
batch sitting at the bound; the archive holds exactly the evicted records,
oldest first; sequence numbers never restart -- neither within the instance
nor across a reload of the trimmed log; the bound is env-configurable with
degrade-to-default on garbage; rotation is best-effort and cannot fail the
append that triggered it; and with nothing configured the default is 50_000
and a small run is never touched (default behaviour unchanged).

``max_history=10`` throughout: the slack is then ``max(1, 10 // 10) == 1``,
so the bound is crossed at the 12th record -- 11 leaves the log alone, 12
trims it to the newest 10.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import DEFAULT_MAX_HISTORY, MAX_HISTORY_ENV, EventStore, default_max_history
from tests.test_store_byte_corruption import FsyncSpy

BASE_TIME = datetime(2026, 4, 9, 7, 15, tzinfo=UTC)


def make_record(index: int) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=f"TLC-HIST-{index:04d}",
            product_description="Romaine Lettuce",
            quantity=100,
            unit_of_measure="cases",
            location_name="Valley Fresh Farms",
            timestamp=BASE_TIME + timedelta(minutes=index),
            kdes={},
        ),
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sequence_nos_in(path: Path) -> list[int]:
    return [line["sequence_no"] for line in _lines(path)]


def _lot_codes_in(path: Path) -> list[str]:
    return [line["event"]["traceability_lot_code"] for line in _lines(path)]


def _bounded_store(tmp_path: Path) -> tuple[EventStore, Path, Path]:
    persist_path = tmp_path / "events.jsonl"
    return EventStore(persist_path=str(persist_path), max_history=10), persist_path, tmp_path / "events.jsonl.1"


def test_the_live_log_is_trimmed_to_the_newest_records_only_once_the_slack_is_crossed(tmp_path):
    """Eleven records sit inside the slack and must not trigger a rewrite --
    rewriting O(max_history) bytes on every batch at the bound is exactly
    the per-append cost the slack exists to avoid. The twelfth crosses it,
    and the log is trimmed to the newest ten, on disk and in memory alike.
    """
    store, persist_path, archive_path = _bounded_store(tmp_path)

    for index in range(1, 12):
        store.add_many([make_record(index)])
    assert len(_lot_codes_in(persist_path)) == 11, "a log inside the slack must be left alone"
    assert not archive_path.exists(), "nothing may be archived before the slack is crossed"

    store.add_many([make_record(12)])

    assert _lot_codes_in(persist_path) == [f"TLC-HIST-{index:04d}" for index in range(3, 13)]
    assert [record.event.traceability_lot_code for record in store.recent()] == [
        f"TLC-HIST-{index:04d}" for index in range(12, 2, -1)
    ], "memory must match the trimmed file, newest first"
    assert store.stats()["total_records"] == 10
    assert [record.sequence_no for record in store.all_between()] == list(range(3, 13))


def test_the_archive_holds_the_evicted_oldest_records_in_order(tmp_path):
    """Across two rotations the archive accumulates exactly what left the live
    log, oldest first, and every record is in precisely one of the two files.
    """
    store, persist_path, archive_path = _bounded_store(tmp_path)

    store.add_many([make_record(index) for index in range(1, 13)])
    assert _sequence_nos_in(archive_path) == [1, 2]
    assert _lot_codes_in(archive_path) == ["TLC-HIST-0001", "TLC-HIST-0002"]

    # 10 retained + 2 more = 12 again: a second rotation appends to the
    # same archive rather than replacing it.
    store.add_many([make_record(index) for index in range(13, 15)])
    assert _sequence_nos_in(archive_path) == [1, 2, 3, 4]
    assert _sequence_nos_in(persist_path) == list(range(5, 15))

    archived = set(_sequence_nos_in(archive_path))
    live = set(_sequence_nos_in(persist_path))
    assert archived.isdisjoint(live)
    assert archived | live == set(range(1, 15)), "a record was lost or duplicated by rotation"


def test_a_fresh_store_over_the_trimmed_log_keeps_the_retained_records_and_the_sequence_high_water_mark(tmp_path):
    """Sequence numbers must not restart -- not in the rotating instance, and
    not in a fresh instance reloading the trimmed file. The newest record is
    always retained, so ``_load_from_disk`` recomputes the same high-water
    mark from it.
    """
    store, persist_path, _ = _bounded_store(tmp_path)
    store.add_many([make_record(index) for index in range(1, 13)])
    (after_trim,) = store.add_many([make_record(13)])
    assert after_trim.sequence_no == 13, "numbering restarted inside the instance that rotated"

    reloaded = EventStore(persist_path=str(persist_path), max_history=10)

    assert [record.sequence_no for record in reloaded.all_between()] == list(range(3, 14))
    assert reloaded._counter == 13
    assert reloaded.stats()["total_records"] == 11
    # The reloaded store rotates on its own next crossing, from the reloaded
    # count, and keeps numbering from the same mark.
    (after_reload,) = reloaded.add_many([make_record(14)])
    assert after_reload.sequence_no == 14
    assert [record.sequence_no for record in reloaded.all_between()] == list(range(5, 15))


def test_a_rotating_append_fsyncs_the_archive_and_the_rewritten_log(tmp_path, monkeypatch):
    """One fsync for the appended batch, one for the archive, then the two
    ``_write_records`` performs (the new file, then its directory): four in
    all, exactly one of them a directory. A non-rotating append does one.
    """
    store, _, _ = _bounded_store(tmp_path)
    store.add_many([make_record(index) for index in range(1, 12)])

    spy = FsyncSpy(os.fsync)
    monkeypatch.setattr(os, "fsync", spy)
    store.add_many([make_record(12)])

    assert len(spy.fds) == 4, f"expected batch + archive + rewritten file + directory, got {len(spy.fds)}"
    assert len(spy.directory_fds) == 1


def test_a_rotation_failure_is_logged_and_does_not_fail_the_append_that_triggered_it(tmp_path, monkeypatch, caplog):
    """The append is acknowledged and on disk before rotation starts, so a
    rotation problem must surface in the log, not as a failed ingest. The
    untrimmed log is the safe outcome: nothing was lost, only not yet bounded.
    """
    store, persist_path, archive_path = _bounded_store(tmp_path)
    store.add_many([make_record(index) for index in range(1, 12)])

    def refuse(_records):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_archive_records", refuse)
    with caplog.at_level(logging.ERROR, logger="inflow_lab"):
        (stored,) = store.add_many([make_record(12)])  # must NOT raise

    assert stored.sequence_no == 12
    assert _sequence_nos_in(persist_path) == list(range(1, 13)), "the append itself must be intact and untrimmed"
    assert not archive_path.exists()
    assert any("rotation failed" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("raw", ["banana", "-5", "0"])
def test_default_max_history_degrades_to_the_default_with_a_warning_on_a_bad_value(monkeypatch, caplog, raw):
    """Same degrade-to-default convention as the tenant cap: this is read at
    import time for the default store, so a typo must warn, never crash."""
    monkeypatch.setenv(MAX_HISTORY_ENV, raw)

    with caplog.at_level(logging.WARNING, logger="inflow_lab"):
        assert default_max_history() == DEFAULT_MAX_HISTORY == 50_000

    assert any(
        MAX_HISTORY_ENV in record.getMessage() and raw in record.getMessage() for record in caplog.records
    ), "a rejected override must be named in the log"


def test_the_env_var_sets_the_bound_for_a_store_not_given_one_explicitly(monkeypatch, tmp_path):
    monkeypatch.setenv(MAX_HISTORY_ENV, "10")
    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))

    assert store.max_history == 10
    store.add_many([make_record(index) for index in range(1, 13)])
    assert len(_lot_codes_in(persist_path)) == 10


def test_an_explicit_argument_wins_over_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv(MAX_HISTORY_ENV, "10")
    assert EventStore(persist_path=str(tmp_path / "events.jsonl"), max_history=25).max_history == 25


def test_an_explicitly_non_positive_bound_is_refused_not_clamped(tmp_path):
    # `records[-0:]` is the whole list, so a silent zero would disable the
    # trim while looking like the tightest possible bound.
    with pytest.raises(ValueError):
        EventStore(persist_path=str(tmp_path / "events.jsonl"), max_history=0)


def test_with_nothing_configured_the_default_is_fifty_thousand_and_a_small_run_is_never_trimmed(
    monkeypatch, tmp_path
):
    """Default behaviour unchanged: no env var means the documented 50_000,
    and a run of ordinary demo size never sees an archive or a rewrite."""
    monkeypatch.delenv(MAX_HISTORY_ENV, raising=False)
    assert default_max_history() == 50_000

    persist_path = tmp_path / "events.jsonl"
    store = EventStore(persist_path=str(persist_path))
    assert store.max_history == 50_000

    store.add_many([make_record(index) for index in range(1, 26)])

    assert len(_lot_codes_in(persist_path)) == 25
    assert not (tmp_path / "events.jsonl.1").exists()

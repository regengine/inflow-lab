"""#217 -- a scenario save is forced to stable storage before it is swapped in.

``ScenarioSaveStore.save`` already wrote through a ``.tmp`` + ``replace``
swap, so a crash mid-write could never leave a half-written save under the
final name. It never fsynced, though: ``replace`` makes the new file visible
atomically, but its bytes could still be sitting in the page cache when the
rename was already durable, and a power loss then leaves an empty or
truncated save under the final name -- worse than the old save it replaced.
Same gap ``EventStore._write_records`` closes for the event log; this pins
the same fix for saves.
"""

from __future__ import annotations

import os

from app.scenario_saves import ScenarioSaveStore
from app.scenarios import ScenarioId
from app.schemas.simulation import SimulationConfig
from tests.test_store_byte_corruption import FsyncSpy


def test_a_scenario_save_is_fsynced_before_the_swap_and_reads_back_intact(tmp_path, monkeypatch):
    """Verified to fail before the fix: ``save`` went through
    ``Path.write_text``, so ``os.fsync`` was never called and ``spy.fds``
    came back empty. The save must also still land under its final name,
    with no temp file left beside it, and read back as what was written.
    """
    save_dir = tmp_path / "saves"
    saves = ScenarioSaveStore(save_dir=str(save_dir))

    spy = FsyncSpy(os.fsync)
    monkeypatch.setattr(os, "fsync", spy)
    saved = saves.save_snapshot(ScenarioId.LEAFY_GREENS_SUPPLIER, SimulationConfig(), records=[])

    assert spy.fds, "save_snapshot returned without forcing the save to stable storage"
    assert (save_dir / "leafy_greens_supplier.json").exists()
    assert list(save_dir.glob("*.tmp")) == [], "the temp file must be swapped away, not left behind"

    reloaded = saves.get(ScenarioId.LEAFY_GREENS_SUPPLIER)
    assert reloaded is not None
    assert reloaded.scenario == saved.scenario
    assert reloaded.config == saved.config
    assert reloaded.records == []

"""Regression tests for tenancy._ensure_persist_path_within_root.

Guards the persist_path traversal fix AND the follow-up regression where the
UI's relative default `data/events.jsonl` was wrongly rejected when
REGENGINE_DATA_DIR is an absolute volume path (broke the browser smoke).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import tenancy


@pytest.fixture()
def abs_data_root(monkeypatch, tmp_path):
    """Simulate a deployment where REGENGINE_DATA_DIR is an ABSOLUTE path that
    is not the current working directory (the Railway volume case)."""
    monkeypatch.setattr(tenancy, "DATA_ROOT", (tmp_path / "volume" / "data"))
    return tmp_path


class TestPersistPathGuard:
    def test_relative_ui_default_is_allowed_under_absolute_root(self, abs_data_root):
        # The UI hardcodes this relative default regardless of DATA_ROOT.
        assert tenancy._ensure_persist_path_within_root("data/events.jsonl") == "data/events.jsonl"

    def test_relative_subpath_allowed(self, abs_data_root):
        assert tenancy._ensure_persist_path_within_root("data/sub/run.jsonl") == "data/sub/run.jsonl"

    def test_absolute_within_configured_root_allowed(self, abs_data_root):
        p = str(abs_data_root / "volume" / "data" / "tenants" / "t" / "events.jsonl")
        assert tenancy._ensure_persist_path_within_root(p) == p

    @pytest.mark.parametrize(
        "escape",
        [
            "/etc/passwd",
            "../../etc/cron.d/inflow",
            "data/../../../tmp/escape.jsonl",
            "/var/lib/other.jsonl",
        ],
    )
    def test_escapes_rejected(self, abs_data_root, escape):
        with pytest.raises(ValueError, match="permitted data directory"):
            tenancy._ensure_persist_path_within_root(escape)

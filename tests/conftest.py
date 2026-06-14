"""Pytest configuration for the inflow-lab test suite.

``tenancy._ensure_persist_path_within_root`` confines any caller-supplied
``persist_path`` to the simulator's data root (``REGENGINE_DATA_DIR``,
default ``data``). Several tests legitimately point ``persist_path`` at
pytest's ``tmp_path``. We route pytest's temporary directories *under* the
data root so those paths stay within the permitted root while remaining
isolated, gitignored test scratch space.
"""

from __future__ import annotations

import os


def pytest_configure(config):
    if not config.option.basetemp:
        data_root = os.getenv("REGENGINE_DATA_DIR", "data")
        config.option.basetemp = os.path.join(data_root, "pytest-tmp")

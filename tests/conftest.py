"""Pytest configuration for the inflow-lab test suite.

``tenancy._ensure_persist_path_within_root`` confines any caller-supplied
``persist_path`` to the simulator's data root (``REGENGINE_DATA_DIR``,
default ``data``). Several tests legitimately point ``persist_path`` at
pytest's ``tmp_path``. We therefore keep pytest's temporary directories
*under* the data root so those paths stay within the permitted root while
remaining isolated, gitignored test scratch space.

We do that by pointing pytest's *temp root* at ``<data root>/pytest-tmp``
rather than by pinning ``--basetemp`` to it. Those are not equivalent: a
caller-supplied ``basetemp`` is deleted and recreated at the start of every
session (``TempPathFactory.getbasetemp``), so two concurrent runs against the
same checkout would wipe each other's ``tmp_path`` files. Feeding the temp
*root* instead leaves pytest's own ``pytest-<n>`` numbered-directory scheme in
charge, which is collision-safe across concurrent runs and garbage-collects
old runs. An explicit ``--basetemp=...`` on the command line still wins.
"""

from __future__ import annotations

import os
from pathlib import Path

#: pytest reads this to decide where to create its ``pytest-of-<user>`` root.
TEMPROOT_ENV = "PYTEST_DEBUG_TEMPROOT"


def pytest_configure(config):
    if config.option.basetemp:
        # The caller pinned a directory; respect it and stay out of the way.
        return
    if os.getenv(TEMPROOT_ENV):
        return
    data_root = Path(os.getenv("REGENGINE_DATA_DIR", "data"))
    temproot = data_root / "pytest-tmp"
    # pytest only mkdirs the `pytest-of-<user>` level, not its parents.
    temproot.mkdir(parents=True, exist_ok=True)
    os.environ[TEMPROOT_ENV] = str(temproot)

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
        # A single fixed basetemp shared by every invocation made the
        # installed pytest delete and recreate that exact directory at the
        # start of every session (_pytest.tmpdir.TempPathFactory.getbasetemp's
        # "given basetemp" branch just rm_rf's + mkdir's it) instead of
        # allocating its own default collision-safe numbered dir. Two
        # concurrent local runs sharing one fixed path would race: the
        # later session's startup wipe deletes the earlier session's
        # still-in-use tmp_path files out from under it (#134). Suffixing
        # with this process's pid keeps every invocation's redirect unique
        # -- concurrent runs get distinct pids and therefore distinct,
        # non-colliding basetemps -- while staying under the data root,
        # which is what satisfies tenancy._ensure_persist_path_within_root
        # for tmp_path-based persist_path values.
        pytest_tmp_root = os.path.join(data_root, "pytest-tmp")
        # getbasetemp()'s own basetemp.mkdir(mode=0o700) call (installed
        # pytest 9.0.3, _pytest/tmpdir.py) does not create parents, so the
        # per-run leaf directory below needs pytest_tmp_root to already
        # exist -- true today only because years of prior fixed-basetemp
        # runs left it behind. exist_ok=True makes this safe both on a
        # from-scratch checkout and against another pytest process
        # creating the same directory concurrently.
        os.makedirs(pytest_tmp_root, exist_ok=True)
        config.option.basetemp = os.path.join(pytest_tmp_root, f"run-{os.getpid()}")

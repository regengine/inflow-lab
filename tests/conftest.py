"""Pytest configuration for the inflow-lab test suite.

Two separate isolation problems are solved here.

1. Where pytest puts its temporary directories (#134)
------------------------------------------------------
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

2. Who owns the checkout's data root while a session runs (#134 follow-up)
--------------------------------------------------------------------------
Giving each session its own ``tmp_path`` tree is not the whole story, because
the parts of the app under test do *not* use ``tmp_path``. The default
(non-tenant) simulation config carries the literal relative default
``persist_path = "data/events.jsonl"`` (``app/schemas/simulation.py``), which
``EventStore`` resolves against the process working directory, and
``EventStore._all_records()`` re-reads that file from disk on every lineage,
stats and export call. Tenant storage (``data/tenants/<id>/``) and scenario
saves (``data/scenario_saves/``) are equally fixed and equally checkout-wide.

So *every* pytest process started in this checkout reads and writes one and
the same event log, no matter what ``--basetemp`` it was given. Two sessions
overlapping by even a second interleave ``store.reset()`` (which unlinks the
log) with another session's ``add_many`` + export, and the reader sees a
record count that belongs to nobody: the symptom we chased was
``test_export_bounds.py::test_fda_export_reports_bounds_when_not_truncated``
failing ``assert '3' == '4'`` while passing in isolation and on the next run.
It is reproducible on demand by starting a second suite run mid-run.

The suite therefore *claims* that shared resource for the duration of a
session, with a blocking exclusive advisory lock. Overlapping sessions queue
instead of corrupting each other. This is a mutex on a genuinely shared
resource, not a retry or a sleep: nothing is re-run and no ordering is
tweaked. The lock is released by the kernel when the process exits, so a
crashed run cannot wedge later ones.

The real fix belongs one level down, in the app: if
``SimulationConfig.persist_path`` defaulted to ``<REGENGINE_DATA_DIR>/events.jsonl``
instead of the hardcoded ``data/events.jsonl``, each session could simply
export its own ``REGENGINE_DATA_DIR`` and be genuinely parallel. That is
``app/schemas/simulation.py`` (plus the tests that pin the literal string) and
is not this file's to change. Until then, serialization is the honest
guarantee.

Set ``REGENGINE_TEST_NO_SUITE_LOCK=1`` to opt out (e.g. when running the suite
against a data root you know is private).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: pytest reads this to decide where to create its ``pytest-of-<user>`` root.
TEMPROOT_ENV = "PYTEST_DEBUG_TEMPROOT"

#: Set to ``1``/``true`` to skip the whole-session data-root lock.
NO_LOCK_ENV = "REGENGINE_TEST_NO_SUITE_LOCK"

#: Lock file guarding the checkout's data root. It lives inside the temp root
#: because that directory is already gitignored (``data/pytest-tmp/``) and
#: already exists for every run; pytest's own garbage collection only touches
#: the ``pytest-of-<user>`` subtree, never this file.
LOCK_FILENAME = ".suite.lock"

_LOCK_STASH_KEY = "_regengine_data_root_lock"


def data_root() -> Path:
    return Path(os.getenv("REGENGINE_DATA_DIR", "data"))


def temp_root() -> Path:
    return data_root() / "pytest-tmp"


def suite_lock_path() -> Path:
    return temp_root() / LOCK_FILENAME


def suite_lock_enabled() -> bool:
    return os.getenv(NO_LOCK_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def _acquire_data_root_lock(config) -> None:
    """Take the checkout's data root exclusively for this session."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX platform
        return

    path = suite_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"waiting for another pytest session to release {path} "
                "(the checkout's data root is shared; see tests/conftest.py)",
                yellow=True,
            )
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    setattr(config, _LOCK_STASH_KEY, handle)


def _release_data_root_lock(config) -> None:
    handle = getattr(config, _LOCK_STASH_KEY, None)
    if handle is None:
        return
    setattr(config, _LOCK_STASH_KEY, None)
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):  # pragma: no cover - best effort
        pass
    finally:
        handle.close()


def pytest_configure(config):
    if not config.option.basetemp and not os.getenv(TEMPROOT_ENV):
        temproot = temp_root()
        # pytest only mkdirs the `pytest-of-<user>` level, not its parents.
        temproot.mkdir(parents=True, exist_ok=True)
        os.environ[TEMPROOT_ENV] = str(temproot)

    if suite_lock_enabled():
        _acquire_data_root_lock(config)


def pytest_unconfigure(config):
    _release_data_root_lock(config)


@pytest.fixture(autouse=True)
def _restore_environment():
    """Undo any environment change a test forgets to undo.

    ``monkeypatch.setenv`` already restores, and today every test uses it --
    this fixture exists so that stays true. A leaked ``REGENGINE_*`` variable
    is invisible in the test that leaks it and only ever fails some later,
    unrelated test, which is the exact failure mode this file is about.
    """
    before = dict(os.environ)
    try:
        yield
    finally:
        if dict(os.environ) != before:
            os.environ.clear()
            os.environ.update(before)

"""The isolation guarantees the suite depends on, asserted rather than assumed.

Background: ``test_export_bounds.py::test_fda_export_reports_bounds_when_not_truncated``
failed intermittently during full-suite runs (``assert '3' == '4'`` on
``X-Export-Total-Records``) while passing in isolation and on the next run.
The cause was not test ordering inside a session -- running the test after
every prefix of the suite never reproduces it -- but two pytest *processes*
sharing this checkout. The default simulation config's ``persist_path`` is the
literal relative ``data/events.jsonl``, which every process in this working
directory resolves to the same file, and ``EventStore._all_records()`` re-reads
that file from disk on every export. One session's ``store.reset()`` unlinks
the log another session has just written four records to.

``tests/conftest.py`` fixes that by taking an exclusive advisory lock on the
data root for the whole session. These tests pin that it is actually held, and
pin the environment-restoration guarantee that keeps the *in-process* half of
the isolation honest.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.schemas.simulation import SimulationConfig

from conftest import data_root, suite_lock_enabled, suite_lock_path


fcntl = pytest.importorskip("fcntl", reason="POSIX advisory locks only")

pytestmark = pytest.mark.skipif(
    not suite_lock_enabled(),
    reason="the data-root lock is disabled for this run",
)


def test_the_running_session_holds_the_data_root_lock_exclusively():
    """No second pytest session can be running against this checkout."""
    lock_path = suite_lock_path()
    assert lock_path.exists(), f"{lock_path} should have been created by pytest_configure"

    script = textwrap.dedent(
        """
        import fcntl, sys
        with open(sys.argv[1], "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("LOCKED")
            else:
                print("FREE")
        """
    )
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", script, str(lock_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "LOCKED", (
        "The session-wide data-root lock is not held. Concurrent pytest runs in "
        "this checkout will interleave writes to the shared event log."
    )


def test_the_lock_is_still_required_because_the_default_event_log_is_checkout_wide():
    """Why the lock exists — and the tripwire for removing it.

    If ``SimulationConfig.persist_path`` ever stops defaulting to a path that
    every process in this working directory shares (e.g. it starts deriving
    from ``REGENGINE_DATA_DIR``), sessions can be isolated by environment
    instead of serialized, and ``tests/conftest.py`` should drop the lock.
    """
    default_path = Path(SimulationConfig().persist_path)
    assert not default_path.is_absolute()
    resolved = (Path.cwd() / default_path).resolve()
    assert resolved.parent == data_root().resolve(), (
        "The default event log moved. Re-read the lock rationale in "
        "tests/conftest.py before changing anything here."
    )


# --- environment restoration ---------------------------------------------
#
# These two run in file order and are a pair: the first leaks an environment
# variable the way a test using bare ``os.environ`` would, the second asserts
# the autouse fixture in conftest cleaned it up.

_LEAKED = "REGENGINE_ISOLATION_PROBE"


def test_a_test_may_leak_an_environment_variable():
    os.environ[_LEAKED] = "leaked"
    assert os.getenv(_LEAKED) == "leaked"


def test_the_leaked_environment_variable_does_not_reach_the_next_test():
    assert _LEAKED not in os.environ


# --- #134: pytest's temp tree stays inside the data root -------------------


def test_tmp_path_stays_inside_the_data_root(request, tmp_path):
    """``tenancy._ensure_persist_path_within_root`` confines caller-supplied
    ``persist_path`` to the data root, and several tests point it at
    ``tmp_path``. That only works while pytest's temp tree lives under the
    data root -- see the ``PYTEST_DEBUG_TEMPROOT`` handling in conftest.
    """
    if request.config.option.basetemp:
        pytest.skip("--basetemp was pinned by the caller; conftest stays out of the way")

    assert os.environ.get("PYTEST_DEBUG_TEMPROOT") == str(data_root() / "pytest-tmp")

    root = data_root().resolve()
    assert root in tmp_path.resolve().parents

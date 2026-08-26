"""The isolation guarantees the suite depends on, asserted rather than assumed.

Background: ``test_export_bounds.py::test_fda_export_reports_bounds_when_not_truncated``
failed intermittently during full-suite runs (``assert '3' == '4'`` on
``X-Export-Total-Records``) while passing in isolation and on the next run.
The cause was not test ordering inside a session -- running the test after
every prefix of the suite never reproduces it -- but two pytest *processes*
sharing this checkout. Every process started here resolves the default event
log to the same file, and ``EventStore._all_records()`` re-reads that file from
disk on every export. One session's ``store.reset()`` unlinks the log another
session has just written four records to.

The default ``persist_path`` no longer hardcodes ``data/events.jsonl``; it
derives from ``REGENGINE_DATA_DIR`` like every other storage path in the app
(#86 follow-up). That removed the *app-level* reason the log was checkout-wide
but not the suite-level one -- see
``test_the_lock_is_still_required_because_every_session_resolves_one_data_root``
below, which pins why.

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

from app import tenancy
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


def test_the_default_event_log_follows_the_configured_data_root(monkeypatch, tmp_path):
    """``persist_path`` derives from ``REGENGINE_DATA_DIR``, resolved per call.

    The production symptom this closes: on Railway the persistent volume is
    mounted at ``REGENGINE_DATA_DIR=/data``, which relocated tenant storage but
    not the default tenant's event log, so those events landed in a
    container-local ``data/`` and were discarded on every redeploy
    (DEPLOYMENT_PROFILES.md step 4). Per *call*, not per import, so a process
    that sets the variable after ``app.tenancy`` is imported still gets it.
    """
    relocated = tmp_path / "volume"
    monkeypatch.setenv("REGENGINE_DATA_DIR", str(relocated))

    assert SimulationConfig().persist_path == str(relocated / "events.jsonl")
    assert tenancy.default_events_path() == relocated / "events.jsonl"

    monkeypatch.delenv("REGENGINE_DATA_DIR")
    assert Path(SimulationConfig().persist_path) == data_root() / "events.jsonl"


def test_the_lock_is_still_required_because_every_session_resolves_one_data_root(
    monkeypatch, tmp_path
):
    """Why the lock survives the fix above — and the tripwire for removing it.

    The obvious next step, once the default log follows ``REGENGINE_DATA_DIR``,
    is for ``tests/conftest.py`` to export a per-session value and let sessions
    run genuinely in parallel instead of queuing. It does not work, and this
    test pins the two facts that stop it, so that anyone who tries gets an
    explanation rather than a mystery.

    1. The data root is chosen by the *invocation*, not by the session. Neither
       documented way of running this suite sets ``REGENGINE_DATA_DIR``, so
       every concurrent session resolves the same checkout-wide ``data/`` —
       one default event log, one tenant tree, one scenario-save directory.
    2. conftest cannot simply relocate it, because
       ``tenancy._ensure_persist_path_within_root`` requires a caller-supplied
       *absolute* ``persist_path`` to sit inside the data root, and dozens of
       tests point ``persist_path`` at ``tmp_path``. conftest only steers
       pytest's temp *root* and deliberately yields to an explicit
       ``--basetemp`` (see ``test_tmp_path_stays_inside_the_data_root``), so a
       caller who pins ``--basetemp=data/...`` puts ``tmp_path`` under the
       checkout no matter what conftest exported. A data root anywhere else
       then turns those tests into HTTP 400s rather than into isolation —
       verified: ``REGENGINE_DATA_DIR=<scratch> pytest --basetemp=data/...
       tests/test_api_delivery.py`` fails 12 of 14.

    Serialization is therefore still the honest guarantee. If that stops being
    true — a per-session root that the guard accepts, or a guard that no longer
    needs one — this test should fail, and the lock in ``tests/conftest.py``
    should go with it.
    """
    # 1. This session shares the checkout's data root with any other session.
    assert "REGENGINE_DATA_DIR" not in os.environ, (
        "This session relocated the data root. If that is now done per session, "
        "the conftest lock is redundant — remove both."
    )
    assert tenancy.default_events_path() == data_root() / "events.jsonl"

    # 2. Relocating it would reject the tmp_path persist_paths the suite uses.
    monkeypatch.setattr(tenancy, "DATA_ROOT", tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="permitted data directory"):
        tenancy._ensure_persist_path_within_root(str(Path.cwd() / "data" / "pinned" / "x.jsonl"))


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

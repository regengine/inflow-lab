"""#161 -- the app must refuse to boot as one of several worker processes.

Simulation run/stop state is per-process: ``SimulationController._task`` /
``_stop_event`` are instance attributes and ``tenancy._tenant_controllers``
is a module-level dict, so a second worker runs its *own* loop that the
first one's Stop cannot reach. Left unguarded, ``POST /api/simulate/stop``
routed to worker B answers 200 while worker A keeps stepping the engine and
delivering events.

The chosen fix is the first branch of the issue's acceptance criterion --
refuse the multi-worker shape at startup and document the single-process
requirement -- so these tests pin two things:

1. the refusal actually fires, for every worker-count signal the process can
   observe, and does *not* fire for a safe or unobservable one; and
2. ``DEPLOYMENT_PROFILES.md`` still documents the requirement the refusal's
   error message sends operators to.

Like ``test_startup_safety.py``, these build their own app via
``create_app()`` rather than importing the ``app.main.app`` singleton: that
singleton is constructed at collection time, before any of these tests can
set the environment under test.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.worker_guard import (
    MULTI_WORKER_OVERRIDE_ENV,
    WEB_CONCURRENCY_ENV,
    enforce_single_process_startup,
    resolve_configured_worker_count,
)


DEPLOYMENT_PROFILES = Path(__file__).resolve().parent.parent / "DEPLOYMENT_PROFILES.md"

# A realistic uvicorn argv, as seen from inside a worker. The leading entry is
# what `_looks_like_a_server_command_line` keys on, and it is present even in a
# spawned worker child -- multiprocessing's spawn context carries the parent's
# sys.argv over and restores it, so `--workers` really is readable from there.
UVICORN_ARGV = ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0"]


@pytest.fixture(autouse=True)
def _neutral_worker_env(monkeypatch: pytest.MonkeyPatch):
    """Pin every input the guard reads, so these tests cannot inherit them.

    The suite's own environment is the thing most likely to make this file
    pass or fail for the wrong reason -- an exported WEB_CONCURRENCY would
    otherwise turn every "starts normally" assertion below into a false
    negative, and the Dockerfile now sets that variable for real.
    """
    monkeypatch.delenv(WEB_CONCURRENCY_ENV, raising=False)
    monkeypatch.delenv(MULTI_WORKER_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv("RAILWAY_REPLICA_ID", raising=False)
    # Auth is unrelated to this guard, but an ambient REGENGINE_REQUIRE_AUTH
    # would make create_app() raise the *other* startup error and mask ours.
    monkeypatch.delenv("REGENGINE_REQUIRE_AUTH", raising=False)


# ---------------------------------------------------------------------------
# The refusal itself, through the real ASGI startup path.
# ---------------------------------------------------------------------------


def test_app_refuses_to_start_when_web_concurrency_is_above_one(monkeypatch):
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "4")

    with pytest.raises(RuntimeError, match="refuses to start with 4 worker processes"):
        with TestClient(create_app()):
            pass


def test_refusal_names_the_cause_the_fix_and_the_override(monkeypatch):
    """A fail-closed startup error is only useful if it is actionable.

    An operator reading this in a deploy log needs to know which knob did it,
    what to set instead, and that a deliberate override exists -- otherwise
    the refusal just looks like a crash.
    """
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "2")

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(create_app()):
            pass

    message = str(excinfo.value)
    assert WEB_CONCURRENCY_ENV in message
    assert MULTI_WORKER_OVERRIDE_ENV in message
    assert "DEPLOYMENT_PROFILES.md" in message
    # The *reason*, not just the rule: this is what stops the next operator
    # from "fixing" it by deleting the guard.
    assert "200" in message and "deliver" in message


def test_app_starts_normally_with_web_concurrency_of_one(monkeypatch):
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "1")

    with TestClient(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200


def test_app_starts_normally_when_nothing_is_configured(monkeypatch):
    """The dev/test default -- no WEB_CONCURRENCY, no flags -- must be unaffected.

    This is the case the whole rest of the suite runs in, several hundred
    times over.
    """
    monkeypatch.delenv(WEB_CONCURRENCY_ENV, raising=False)

    with TestClient(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200


def test_override_env_var_allows_multi_worker_startup_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "4")
    monkeypatch.setenv(MULTI_WORKER_OVERRIDE_ENV, "1")
    caplog.set_level(logging.WARNING, logger="inflow_lab")

    with TestClient(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    # Overriding must stay loud: the hazard is still real, the operator has
    # just accepted it.
    assert any(MULTI_WORKER_OVERRIDE_ENV in message and "4 worker" in message for message in messages)


@pytest.mark.parametrize("falsy_value", ["", "0", "false", "no", "off", "maybe"])
def test_override_is_off_unless_explicitly_truthy(monkeypatch, falsy_value):
    """Default-safe: only the documented truthy spellings disarm the guard."""
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "4")
    monkeypatch.setenv(MULTI_WORKER_OVERRIDE_ENV, falsy_value)

    with pytest.raises(RuntimeError, match="refuses to start"):
        enforce_single_process_startup(argv=UVICORN_ARGV)


@pytest.mark.parametrize("truthy_value", ["1", "true", "True", "yes", "ON"])
def test_override_accepts_common_true_spellings(monkeypatch, truthy_value):
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "4")
    monkeypatch.setenv(MULTI_WORKER_OVERRIDE_ENV, truthy_value)

    enforce_single_process_startup(argv=UVICORN_ARGV)  # must not raise


# ---------------------------------------------------------------------------
# Which signals the process can actually read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        ["--workers", "4"],
        ["--workers=4"],
        ["-w", "4"],
        ["-w4"],
    ],
)
def test_worker_flag_on_the_command_line_is_detected(monkeypatch, flags):
    """Covers uvicorn's `--workers` and gunicorn's `-w`, in each spelling.

    Readable from a uvicorn worker child because multiprocessing's spawn
    context restores the parent's argv there, and from a gunicorn worker
    because gunicorn forks.
    """
    resolved = resolve_configured_worker_count(argv=UVICORN_ARGV + flags)

    assert resolved.count == 4
    assert "--workers" in resolved.source


def test_explicit_single_worker_flag_beats_web_concurrency(monkeypatch):
    """`WEB_CONCURRENCY=4 uvicorn ... --workers 1` really does run one worker.

    uvicorn only falls back to WEB_CONCURRENCY when the flag is absent
    (`if workers is None and "WEB_CONCURRENCY" in os.environ`), so refusing
    here would be a false alarm on a genuinely safe configuration.
    """
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, "4")

    resolved = resolve_configured_worker_count(argv=UVICORN_ARGV + ["--workers", "1"])
    assert resolved.count == 1

    enforce_single_process_startup(argv=UVICORN_ARGV + ["--workers", "1"])  # must not raise


def test_worker_flag_is_ignored_outside_a_server_command_line(monkeypatch):
    """The guard must not read a `--workers`-shaped token out of any old argv.

    This is what keeps a test runner or unrelated tool from tripping a
    fail-closed startup check. Only a uvicorn/gunicorn argv is consulted.
    """
    resolved = resolve_configured_worker_count(
        argv=["/usr/bin/some-other-tool", "--workers", "8"],
    )

    assert resolved.count == 1


def test_the_real_pytest_argv_does_not_trip_the_guard():
    """The suite starts this app in-process many times; it must stay startable.

    Asserted against this process's genuine ``sys.argv`` rather than a
    hand-written one, so it keeps holding for however the suite is actually
    invoked.
    """
    assert resolve_configured_worker_count(argv=list(sys.argv)).count == 1
    enforce_single_process_startup(argv=list(sys.argv))  # must not raise


@pytest.mark.parametrize("malformed", ["four", "", "  ", "1.5"])
def test_malformed_web_concurrency_is_ignored_rather_than_guessed_at(monkeypatch, malformed):
    monkeypatch.setenv(WEB_CONCURRENCY_ENV, malformed)

    assert resolve_configured_worker_count(argv=UVICORN_ARGV).count == 1


def test_railway_deployment_logs_the_single_replica_requirement(monkeypatch, caplog):
    """The one gap the guard cannot close, surfaced where operators look.

    Railway publishes RAILWAY_REPLICA_ID and RAILWAY_REPLICA_REGION but no
    replica *count*, so a replica cannot tell whether it is 1 of 1 or 1 of 4.
    Scaling the service is therefore undetectable from in here; the deploy
    log is the only place left to state the requirement.
    """
    monkeypatch.setenv("RAILWAY_REPLICA_ID", "replica-abc123")
    caplog.set_level(logging.INFO, logger="inflow_lab")

    enforce_single_process_startup(argv=UVICORN_ARGV)

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    assert any("ONE replica" in message for message in messages)


def test_no_railway_log_line_off_railway(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="inflow_lab")

    enforce_single_process_startup(argv=UVICORN_ARGV)

    messages = [record.getMessage() for record in caplog.records if record.name == "inflow_lab"]
    assert not any("ONE replica" in message for message in messages)


# ---------------------------------------------------------------------------
# The documentation half of the acceptance criteria.
# ---------------------------------------------------------------------------


def test_deployment_profiles_documents_the_single_process_requirement():
    """#161's second acceptance criterion, pinned.

    The refusal's error message sends the operator to DEPLOYMENT_PROFILES.md,
    so that page going quiet about this would break the one instruction the
    failure gives them.
    """
    assert DEPLOYMENT_PROFILES.is_file(), f"expected {DEPLOYMENT_PROFILES}"
    text = DEPLOYMENT_PROFILES.read_text(encoding="utf-8")

    assert WEB_CONCURRENCY_ENV in text, (
        "DEPLOYMENT_PROFILES.md must name WEB_CONCURRENCY -- it is the variable that turns "
        "the shipped container into a multi-worker deployment, and the one the startup "
        "refusal tells operators to set to 1."
    )
    assert MULTI_WORKER_OVERRIDE_ENV in text, (
        "DEPLOYMENT_PROFILES.md must document the override the error message advertises."
    )
    assert "replica" in text.lower(), (
        "DEPLOYMENT_PROFILES.md must state the single-replica requirement: Railway exposes "
        "no replica-count variable, so documentation is the only control for that half."
    )

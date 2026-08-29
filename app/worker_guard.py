"""Refuse to start when this process is one of several serving one simulator.

#161. The simulator's run/stop control plane is *per-process memory*, with no
persisted or shared representation anywhere:

- ``SimulationController.running`` is a property over ``self._task``
  (``app/controller.py``), and ``self._task``/``self._stop_event`` are plain
  instance attributes assigned by ``start()`` and signalled by ``stop()``.
- ``tenancy.controller`` and ``tenancy._tenant_controllers`` are module-level
  singletons, built once per interpreter, and
  ``tenancy.get_tenant_controller_for_id`` mints new controllers into that
  one process's dict.

So a second worker or replica does not share the first one's run loop -- it
has its *own*, and neither can see the other's. ``POST /api/simulate/stop``
routed to worker B sets worker B's stop event, finds no task there, and
answers 200; worker A's loop keeps stepping the engine and keeps delivering
events, including live RegEngine traffic. The caller is told the simulation
stopped while it demonstrably has not.

Real cross-process coordination (a shared registry plus a distributed lock)
is the wrong trade for a deliberately non-production simulator whose event
store is an in-memory deque -- see REPO_PURPOSE.md. The supported answer is
that this app is single-process, and this module makes that a startup
precondition instead of a convention nobody wrote down.

What is actually detectable from inside the process is a narrow set, and
this module deliberately claims no more than it can prove -- see
``resolve_configured_worker_count`` and the "what this guard cannot see"
note below.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass


logger = logging.getLogger("inflow_lab")


# The uvicorn/gunicorn/Heroku convention for "how many worker processes".
# uvicorn reads it as the default for --workers when the flag is absent
# (uvicorn/config.py: `if workers is None and "WEB_CONCURRENCY" in os.environ`),
# which is exactly the shape of the Dockerfile CMD -- so this variable alone
# is enough to turn the shipped image into a multi-worker deployment.
WEB_CONCURRENCY_ENV = "WEB_CONCURRENCY"

# Documented escape hatch (#161). Default-off, so the safe configuration is
# the one you get by doing nothing. Named to match the repo's existing
# REGENGINE_ALLOW_* opt-outs (cf. REGENGINE_ALLOW_PRIVATE_ENDPOINTS), and
# parsed with the same truthy spellings.
MULTI_WORKER_OVERRIDE_ENV = "REGENGINE_ALLOW_MULTIPLE_WORKERS"

_TRUTHY = {"1", "true", "yes", "on"}

# Worker-count flags we can read off the command line. uvicorn only spells it
# `--workers`; gunicorn accepts `-w` as well.
_LONG_FLAG = "--workers"
_SHORT_FLAG = "-w"

# What this guard CANNOT see, and why. Recorded here because the failure mode
# of a partial guard is someone assuming it is a total one; each of these is
# documented as an operator responsibility in DEPLOYMENT_PROFILES.md instead.
#
# - Railway replica count. Railway provides RAILWAY_REPLICA_ID and
#   RAILWAY_REPLICA_REGION per replica but no replica *count* anywhere in its
#   provided-variable set (docs.railway.com/variables/reference), so a replica
#   cannot tell whether it is 1 of 1 or 1 of 4. Railway also load-balances
#   public traffic randomly with no sticky sessions, which is exactly what
#   lets a Stop land on a different replica than the running loop. The one
#   thing available here is to log the requirement, which the bottom of
#   enforce_single_process_startup does.
# - Several containers/hosts behind any load balancer: each is a healthy
#   single-worker process and none of them can observe the others.
# - gunicorn's GUNICORN_CMD_ARGS. gunicorn is not a dependency of this
#   project and is not installed, so a guard for it could not be exercised.
#   Its `-w N` on the real command line *is* covered, because gunicorn forks
#   and the worker keeps the original argv.
# - uvicorn.run(..., workers=N) from a custom Python entrypoint: the count is
#   a function argument, present in neither argv nor the environment.


@dataclass(frozen=True)
class WorkerCount:
    """How many worker processes this deployment is configured for, and why."""

    count: int
    source: str


def _override_enabled() -> bool:
    return os.getenv(MULTI_WORKER_OVERRIDE_ENV, "").strip().lower() in _TRUTHY


def _looks_like_a_server_command_line(argv: list[str]) -> bool:
    """True when argv belongs to a uvicorn/gunicorn launch.

    Gating the argv scan on this is what keeps the guard from misreading an
    unrelated tool's `-w`. It matters most for pytest: the suite builds this
    app in-process many times, and a guard that read any `--workers`-shaped
    token out of an arbitrary argv could be tripped by a test runner flag
    rather than by a real deployment.
    """
    return any("uvicorn" in token.lower() or "gunicorn" in token.lower() for token in argv)


def _worker_count_from_argv(argv: list[str]) -> int | None:
    """Read an explicit worker-count flag off *argv*, or None if absent.

    This works even in a uvicorn worker child, which is not obvious and is
    the reason the guard can catch `--workers N` at all. uvicorn spawns
    workers through ``multiprocessing`` with the *spawn* context
    (uvicorn/_subprocess.py), and ``multiprocessing.spawn`` ships the parent's
    ``sys.argv`` across in its preparation data and restores it in the child.
    Verified empirically against uvicorn 0.46: a child of
    ``uvicorn app.main:app --workers 4`` sees the full original argv,
    ``--workers 4`` included.

    Last occurrence wins, matching how click and argparse resolve a repeated
    option. A flag whose value is not an integer is skipped rather than
    guessed at -- uvicorn will fail on it with its own clearer error.
    """
    if not _looks_like_a_server_command_line(argv):
        return None

    count: int | None = None
    for index, token in enumerate(argv):
        raw: str | None = None
        if token in (_LONG_FLAG, _SHORT_FLAG):
            if index + 1 < len(argv):
                raw = argv[index + 1]
        elif token.startswith(f"{_LONG_FLAG}="):
            raw = token.split("=", 1)[1]
        elif token.startswith(_SHORT_FLAG) and token[len(_SHORT_FLAG) :].isdigit():
            # click accepts an attached short value, e.g. `-w4`.
            raw = token[len(_SHORT_FLAG) :]

        if raw is None:
            continue
        try:
            count = int(raw.strip())
        except ValueError:
            continue
    return count


def _worker_count_from_env() -> int | None:
    raw = os.getenv(WEB_CONCURRENCY_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        # uvicorn does an unguarded int() on this and will raise its own
        # error; nothing useful for us to add, and guessing a count from a
        # malformed value would be worse than ignoring it.
        return None


def resolve_configured_worker_count(argv: list[str] | None = None) -> WorkerCount:
    """How many workers this process's launcher was configured to run.

    Precedence mirrors uvicorn's and gunicorn's own: an explicit ``--workers``
    flag beats ``WEB_CONCURRENCY``, which beats the default of 1. Modelling
    that faithfully matters -- ``WEB_CONCURRENCY=4 uvicorn ... --workers 1``
    really does run a single worker, and refusing to boot on it would be a
    false alarm on a safe configuration.
    """
    command_line = list(sys.argv if argv is None else argv)

    from_argv = _worker_count_from_argv(command_line)
    if from_argv is not None:
        return WorkerCount(from_argv, f"the {_LONG_FLAG} flag on this process's command line")

    from_env = _worker_count_from_env()
    if from_env is not None:
        return WorkerCount(from_env, f"the {WEB_CONCURRENCY_ENV} environment variable")

    return WorkerCount(1, "the default (no worker-count flag or variable set)")


def enforce_single_process_startup(argv: list[str] | None = None) -> None:
    """Raise unless this deployment is configured to run exactly one process.

    Called from the lifespan hook, so it runs inside *every* worker: with
    ``--workers 4`` all four children refuse and the deploy fails loudly,
    rather than three of them quietly serving a control plane that the fourth
    cannot stop.
    """
    resolved = resolve_configured_worker_count(argv)

    if resolved.count > 1:
        if _override_enabled():
            logger.warning(
                "%s is set, so inflow-lab is starting with %d worker processes. Simulation "
                "run/stop state is per-process: Start and Stop only affect the worker that "
                "serves the request, so a Stop can return 200 while another worker keeps "
                "running the simulation and delivering events. See DEPLOYMENT_PROFILES.md.",
                MULTI_WORKER_OVERRIDE_ENV,
                resolved.count,
            )
        else:
            raise RuntimeError(
                f"inflow-lab refuses to start with {resolved.count} worker processes "
                f"(configured by {resolved.source}). Simulation run/stop state lives in "
                "per-process memory, so with more than one worker a Stop request can return "
                "200 while a different worker keeps running the simulation and delivering "
                f"events. Run a single process: set {WEB_CONCURRENCY_ENV}=1 (and drop any "
                f"{_LONG_FLAG} flag above 1). To override deliberately, set "
                f"{MULTI_WORKER_OVERRIDE_ENV}=1. See DEPLOYMENT_PROFILES.md."
            )

    if os.getenv("RAILWAY_REPLICA_ID"):
        # The one signal available for the gap this guard cannot close.
        # Railway publishes a replica *id* but never a replica *count*, so
        # scaling this service past one replica is undetectable from in here.
        # Saying so at boot puts the requirement in the deploy logs, where an
        # operator scaling the service is actually looking.
        logger.info(
            "inflow-lab is running on Railway and requires exactly ONE replica. Simulation "
            "run/stop state is per-process and Railway load-balances across replicas without "
            "sticky sessions, so a second replica makes Stop unreliable. Railway exposes no "
            "replica-count variable, so this process cannot verify the setting -- keep the "
            "service's replica count at 1. See DEPLOYMENT_PROFILES.md."
        )

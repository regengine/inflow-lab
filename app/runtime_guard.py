"""Process-model invariants the simulator must hold before a controller exists.

Split out of ``controller.py``: nothing here touches controller state, a lock
or the event loop. It reads environment variables and raises. Keeping it
beside the snapshot/deliver/commit machinery only made that file longer.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping


logger = logging.getLogger(__name__)


# Simulation run state -- `running`, `_task`, `_stop_event` -- and the tenant
# controller registry in `tenancy.py` live in one process's memory. There is no
# shared coordination point, so under two or more workers each request lands on
# an arbitrary process: a Stop can return 200 while a *different* worker's run
# loop keeps generating and delivering events, including live traffic.
#
# Single-process is therefore a hard requirement, not a coincidence of the
# current Dockerfile CMD happening to omit `--workers`. Rather than leave it as
# an accident that a later `WEB_CONCURRENCY=4` or a Railway replica bump would
# silently break, the invariant is enforced here: a controller refuses to exist
# in a process that was told to run alongside siblings. See
# DEPLOYMENT_PROFILES.md ("Single-process requirement").
#
# Moving run/stop state to a shared coordination point (so Stop works from any
# worker) is the larger fix and remains open; this makes the current limit
# loud instead of silent.
WORKER_COUNT_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")
REPLICA_COUNT_ENV_VARS = ("RAILWAY_REPLICA_COUNT", "WEB_REPLICAS")

__all__ = [
    "REPLICA_COUNT_ENV_VARS",
    "WORKER_COUNT_ENV_VARS",
    "MultiProcessRuntimeError",
    "enforce_single_process_runtime",
]


class MultiProcessRuntimeError(RuntimeError):
    """Raised when the process is configured to run alongside sibling workers."""


def _configured_process_count(name: str, environ: Mapping[str, str]) -> int | None:
    """Parse a worker/replica count env var, ignoring unset or malformed values."""
    raw_value = (environ.get(name) or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Ignoring malformed %s=%r when checking worker count", name, raw_value)
        return None


def enforce_single_process_runtime(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to run in a process configured for multi-worker/multi-replica.

    Only an *explicit* count above 1 trips this: an unset or malformed value
    is the single-process default and is allowed through, so the documented
    local and Railway profiles keep working untouched.
    """
    environ = os.environ if environ is None else environ
    for name in (*WORKER_COUNT_ENV_VARS, *REPLICA_COUNT_ENV_VARS):
        count = _configured_process_count(name, environ)
        if count is not None and count > 1:
            raise MultiProcessRuntimeError(
                f"{name}={count} would run the simulator in more than one process, but "
                "simulation run/stop state is per-process: a Stop request could return "
                "200 while another process keeps delivering events. Run a single worker "
                "and a single replica (see DEPLOYMENT_PROFILES.md)."
            )

"""The shipped container profile is itself a security control (#88, #180).

Both fixes live entirely in `Dockerfile`, so the rest of the suite cannot see
them: deleting either line leaves every other test green while changing what
the image actually does.

- #88  -- `REGENGINE_REQUIRE_AUTH=1` is what opts the container into the
          fail-closed startup check in `app/main.py`. `test_startup_safety.py`
          proves the app refuses to boot *when the variable is set*; nothing
          else proves the image sets it. Without it a container boots on a
          warning and serves the whole state-changing API on 0.0.0.0.
- #180 -- the `exec` in the CMD is what makes uvicorn the container's PID 1.
          Drop it and the shell stays PID 1, does not forward SIGTERM, and the
          lifespan shutdown never runs on stop or redeploy.

These are one-word deletions a future edit could make by accident, with no
failing test to catch either. Hence this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    assert DOCKERFILE.is_file(), f"expected a Dockerfile at {DOCKERFILE}"
    return DOCKERFILE.read_text(encoding="utf-8")


def test_container_image_requires_basic_auth(dockerfile_text):
    # Matched against the ENV instruction rather than a bare substring so a
    # mention in a comment cannot satisfy it.
    env_block = re.search(
        r"^ENV\s+(.*?)(?=^\s*(?:FROM|RUN|COPY|WORKDIR|EXPOSE|CMD|ENTRYPOINT|HEALTHCHECK|ARG|USER)\b)",
        dockerfile_text,
        re.MULTILINE | re.DOTALL,
    )
    assert env_block is not None, "no ENV instruction found in the Dockerfile"
    assert "REGENGINE_REQUIRE_AUTH=1" in env_block.group(1), (
        "The container profile must set REGENGINE_REQUIRE_AUTH=1 (#88). Without it the "
        "image binds 0.0.0.0 and boots without Basic Auth on nothing but a log warning. "
        "app/main.py's fail-closed check is opt-in by design so a local `uvicorn --reload` "
        "still starts; the Dockerfile is what opts the shared/remote profile in."
    )


def test_container_cmd_execs_uvicorn_so_it_becomes_pid_1(dockerfile_text):
    cmd_lines = [
        line for line in dockerfile_text.splitlines() if line.strip().startswith("CMD [")
    ]
    assert cmd_lines, "no exec-form CMD instruction found in the Dockerfile"
    cmd = cmd_lines[-1]

    assert "uvicorn" in cmd, f"expected the CMD to start uvicorn, got: {cmd}"
    assert re.search(r"exec\s+uvicorn", cmd), (
        "The Dockerfile CMD must `exec uvicorn` (#180). Running it through `sh -c` without "
        "`exec` leaves the shell as PID 1 with uvicorn as its child; a shell at PID 1 does "
        "not forward SIGTERM, so container stop and every redeploy skip the graceful "
        f"shutdown and the lifespan hook entirely. Got: {cmd}"
    )

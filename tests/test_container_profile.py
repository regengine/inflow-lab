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
- #126 -- the base image's Python version has to stay inside `ci.yml`'s pytest
          matrix. The two files are edited independently, so a base bump is
          exactly the change that silently moves the deployed runtime off the
          one CI tests.
- #161 -- `WEB_CONCURRENCY=1` pins the single-process requirement into the
          image instead of leaving it to uvicorn's implicit default. Simulation
          run/stop state is per-process, so a second worker serves a control
          plane that cannot stop the first one's run loop.

These are one-word deletions a future edit could make by accident, with no
failing test to catch either. Hence this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Every `python-version:` value in the workflow, in either YAML list form
# (`['3.11', '3.12']` or a `- '3.11'` block) or as a bare scalar. Deliberately
# regex rather than a YAML parse: this suite's dependency group has no YAML
# library, and PyYAML is only ever present here as a transitive dependency of
# bandit, which is not a contract worth resting a test on.
_PYTHON_VERSION_DECLARATION = re.compile(
    r"^[ \t]*python-version:[ \t]*(?P<inline>\[[^\]]*\]|[^\n#]*)"
    r"(?P<block>(?:\n[ \t]*-[ \t]*[^\n#]*)*)",
    re.MULTILINE,
)


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    assert DOCKERFILE.is_file(), f"expected a Dockerfile at {DOCKERFILE}"
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_workflow_text() -> str:
    assert CI_WORKFLOW.is_file(), f"expected a CI workflow at {CI_WORKFLOW}"
    return CI_WORKFLOW.read_text(encoding="utf-8")


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


def test_container_image_pins_a_single_worker_process(dockerfile_text):
    # Same ENV-instruction match as above, so the explanatory comment sitting
    # above the ENV block cannot satisfy this on its own.
    env_block = re.search(
        r"^ENV\s+(.*?)(?=^\s*(?:FROM|RUN|COPY|WORKDIR|EXPOSE|CMD|ENTRYPOINT|HEALTHCHECK|ARG|USER)\b)",
        dockerfile_text,
        re.MULTILINE | re.DOTALL,
    )
    assert env_block is not None, "no ENV instruction found in the Dockerfile"
    assert "WEB_CONCURRENCY=1" in env_block.group(1), (
        "The container profile must set WEB_CONCURRENCY=1 (#161). The CMD passes no "
        "--workers flag, so uvicorn takes its worker count straight from this variable; "
        "pinning it states the single-process requirement in the image instead of relying "
        "on uvicorn's implicit default. Simulation run/stop state is per-process, so a "
        "second worker answers Stop with a 200 while the first keeps delivering events."
    )


def test_container_cmd_leaves_the_worker_count_to_the_environment(dockerfile_text):
    """The CMD must NOT hard-code --workers, even to 1 (#161).

    An explicit `--workers 1` outranks WEB_CONCURRENCY in both uvicorn and
    gunicorn. That would look safer while actually being worse: an operator
    who sets WEB_CONCURRENCY=4 on the platform would silently get one worker
    and no error, instead of the startup refusal that tells them this app
    cannot be scaled that way. Keeping the flag out is what lets the variable
    reach app/worker_guard.py and fail loudly.
    """
    cmd_lines = [line for line in dockerfile_text.splitlines() if line.strip().startswith("CMD [")]
    assert cmd_lines, "no exec-form CMD instruction found in the Dockerfile"

    assert not re.search(r"--workers|\s-w\b", cmd_lines[-1]), (
        "The Dockerfile CMD must not pass a worker-count flag; set WEB_CONCURRENCY in ENV "
        f"instead so the startup guard can see it. Got: {cmd_lines[-1]}"
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


def test_ci_runs_the_python_version_the_image_ships(dockerfile_text, ci_workflow_text):
    base = re.search(r"^FROM\s+python:(\d+\.\d+)[-\s]", dockerfile_text, re.MULTILINE)
    assert base is not None, (
        "expected the Dockerfile to start from an explicitly versioned python "
        "base image (e.g. `FROM python:3.12-slim`); without a pinned minor "
        "version there is nothing for CI to match."
    )
    image_python = base.group(1)

    declared = {
        version
        for match in _PYTHON_VERSION_DECLARATION.finditer(ci_workflow_text)
        for version in re.findall(
            r"\d+\.\d+", match.group("inline") + match.group("block")
        )
    }
    assert declared, (
        f"found no python-version values in {CI_WORKFLOW}; if the workflow now "
        "declares its interpreter some other way, update this test rather than "
        "deleting it -- the coupling it guards is still real."
    )

    assert image_python in declared, (
        f"The deployed image is FROM python:{image_python} but ci.yml only tests "
        f"{sorted(declared)} (#126). CI would then be green on a Python the "
        "deploy never runs, which is how a 3.12-only break reached Railway "
        f"instead of a pull request. Add '{image_python}' to the pytest matrix, "
        "or move the image to a version CI already covers."
    )

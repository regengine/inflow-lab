"""Guards on the workflow definitions themselves.

Two failures these lock down, both of which are invisible until someone tries
to merge or until an unsupervised agent run goes wrong:

1. Branch protection requires status checks named exactly `pytest` and
   `browser-smoke`. Wrapping either job in a `strategy.matrix` renames the
   checks it reports (`pytest (3.11)`, `pytest (3.12)`), so the required check
   is never reported, stays permanently "expected", and NO pull request in the
   repo can merge -- GitHub answers `405: 2 of 2 required status checks have
   not succeeded`. That is exactly what a Python matrix over `pytest` did.

2. codex-autopilot runs an unsupervised agent with contents:write and opens
   every PR under the same generic title. Letting `.github/workflows/**` into
   its add-paths meant an agent-authored change to its own permissions could
   ride along unremarked (#128).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: Job names branch protection requires by exact string. Renaming one, or
#: turning it into a matrix, blocks every merge in the repository.
REQUIRED_CHECK_NAMES = ("pytest", "browser-smoke")


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci_jobs() -> dict:
    return load("ci.yml")["jobs"]


@pytest.mark.parametrize("job_name", REQUIRED_CHECK_NAMES)
def test_required_check_job_exists(ci_jobs, job_name):
    assert job_name in ci_jobs, (
        f"branch protection requires a status check named exactly {job_name!r}; "
        "renaming the job makes that check permanently unreported and blocks "
        "every merge"
    )


@pytest.mark.parametrize("job_name", REQUIRED_CHECK_NAMES)
def test_required_check_job_is_not_a_matrix(ci_jobs, job_name):
    job = ci_jobs[job_name]
    assert "matrix" not in (job.get("strategy") or {}), (
        f"{job_name} must not use strategy.matrix: GitHub would report its "
        f"checks as '{job_name} (<value>)' and the required {job_name!r} check "
        "would never appear. Add a separate job instead."
    )


@pytest.mark.parametrize("job_name", REQUIRED_CHECK_NAMES)
def test_required_check_job_is_not_renamed_by_a_name_key(ci_jobs, job_name):
    """A `name:` on the job overrides the check name just as a matrix does."""
    declared = ci_jobs[job_name].get("name")
    assert declared in (None, job_name), (
        f"{job_name} declares name: {declared!r}, which is the string GitHub "
        "reports as the check name"
    )


def test_python_312_coverage_is_still_present(ci_jobs):
    """#126 wanted the suite on 3.12 too; splitting the matrix must not lose it."""
    versions = set()
    for job in ci_jobs.values():
        for step in job.get("steps", []):
            with_block = step.get("with") or {}
            if "python-version" in with_block:
                versions.add(str(with_block["python-version"]))
    assert "3.11" in versions and "3.12" in versions


def test_dependency_health_runs_exactly_once(ci_jobs):
    steps = [
        step
        for job in ci_jobs.values()
        for step in job.get("steps", [])
        if "pip-audit" in str(step.get("run", ""))
    ]
    assert len(steps) == 1, "the dependency-health pass should run once, not per interpreter"


def test_autopilot_cannot_include_workflow_edits_in_its_pr():
    jobs = load("codex-autopilot.yml")["jobs"]
    steps = [step for job in jobs.values() for step in job.get("steps", [])]
    pr_steps = [
        step for step in steps if "create-pull-request" in str(step.get("uses", ""))
    ]
    assert pr_steps, "codex-autopilot no longer opens a PR; this guard needs updating"
    for step in pr_steps:
        add_paths = (step.get("with") or {}).get("add-paths", "")
        entries = [line.strip() for line in add_paths.splitlines() if line.strip()]
        assert ".github/**" not in entries, (
            "an unrestricted .github/** lets the unsupervised agent's own "
            "workflow permissions change inside a PR titled 'Autopilot "
            "maintenance'; list the .github subpaths it may touch instead"
        )
        assert not any(entry.startswith(".github/workflows") for entry in entries)


def test_autopilot_reverts_and_flags_workflow_edits():
    jobs = load("codex-autopilot.yml")["jobs"]
    steps = [step for job in jobs.values() for step in job.get("steps", [])]
    guards = [step for step in steps if step.get("id") == "workflow_guard"]
    assert len(guards) == 1, "the workflow guard step is missing from codex-autopilot"
    assert "git checkout -- .github/workflows" in guards[0]["run"]

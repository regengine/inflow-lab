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


def workflow_events(workflow: dict) -> dict:
    # PyYAML 1.1 interprets the plain scalar ``on`` as boolean true.
    return workflow.get("on", workflow.get(True, {}))


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


def test_contract_gate_uses_trusted_base_code_for_credentialed_pr_metadata_checks():
    workflow = load("contract-pin-drift.yml")
    events = workflow_events(workflow)
    job = workflow["jobs"]["drift"]
    steps = job["steps"]

    assert "pull_request_target" in events
    assert "pull_request" not in events

    checkout_steps = [step for step in steps if "actions/checkout" in str(step.get("uses", ""))]
    assert len(checkout_steps) == 1
    checkout = checkout_steps[0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False

    serialized = yaml.safe_dump(job)
    assert "pull_request.head" not in serialized
    assert "pull_request.base" not in serialized
    assert "github.head_ref" not in serialized
    assert "refs/pull" not in serialized
    assert "actions/download-artifact" not in serialized
    assert "uv sync" not in serialized
    assert "pip install" not in serialized


def test_contract_gate_manual_trigger_can_only_run_the_default_branch_workflow():
    """``workflow_dispatch --ref`` may select a branch-authored workflow.

    ``repository_dispatch`` is deliberately used for the maintainer-triggered
    run because GitHub only loads that event's workflow from the default
    branch.  That invariant matters while the job can read the cross-repo
    token.
    """
    events = workflow_events(load("contract-pin-drift.yml"))

    assert "workflow_dispatch" not in events
    assert events["repository_dispatch"] == {"types": ["contract-pin-drift"]}


def test_enforced_contract_runs_fail_when_cross_repo_access_is_unconfigured():
    job = load("contract-pin-drift.yml")["jobs"]["drift"]
    access = next(step for step in job["steps"] if step.get("id") == "access")
    script = access["run"]

    assert '[ "${has_access}" != "true" ] && [ "${enforce}" = "true" ]' in script
    assert "exit 1" in script
    assert "NOT CHECKED" in script


def test_checked_output_is_set_only_after_both_directions_and_contract_provenance_complete():
    job = load("contract-pin-drift.yml")["jobs"]["drift"]

    assert job["outputs"]["checked"] == "${{ steps.complete.outputs.checked }}"
    complete = next(step for step in job["steps"] if step.get("id") == "complete")
    condition = complete["if"]
    assert "steps.simulator_drift.outputs.completed == 'true'" in condition
    assert "steps.regengine_drift.outputs.completed == 'true'" in condition
    assert "steps.contract_provenance.outputs.completed == 'true'" in condition
    assert "checked=true" in complete["run"]


def test_trusted_contract_gate_reextracts_the_real_table_and_version_from_an_immutable_ref():
    job = load("contract-pin-drift.yml")["jobs"]["drift"]
    steps = job["steps"]
    source = next(step for step in steps if step.get("id") == "contract_source")
    provenance = next(step for step in steps if step.get("id") == "contract_provenance")
    script = provenance["run"]

    assert "scripts/regengine_kde_contract.py verify" in script
    assert "steps.regengine_ref.outputs.sha" in yaml.safe_dump(provenance)
    assert '-f ref="${REGENGINE_SHA}"' in source["run"]
    assert "Verified RegEngine source commit ${REGENGINE_SHA}" in script
    assert "exact source-byte SHA-256 before AST extraction" in script
    assert "python" in script

    # The private-read token exists only in fixed fetch steps. The byte
    # provenance check and AST parser/verifier run later without credentials
    # or upstream execution.
    assert "GH_TOKEN" in source["env"]
    assert "python" not in source["run"]
    assert "GH_TOKEN" not in provenance.get("env", {})


def test_contract_gate_validates_config_before_writing_step_outputs():
    job = load("contract-pin-drift.yml")["jobs"]["drift"]
    access = next(step for step in job["steps"] if step.get("id") == "access")
    script = access["run"]

    assert "Invalid REGENGINE_CONTRACT_REPOSITORY" in script
    assert "Invalid REGENGINE_CONTRACT_REF" in script
    assert script.index("Invalid REGENGINE_CONTRACT_REPOSITORY") < script.index("regengine_repository=${repository}")

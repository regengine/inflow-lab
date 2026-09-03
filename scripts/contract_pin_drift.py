"""Decide whether a cross-repo contract pin has drifted too far.

Two pins hold the inflow-lab <-> RegEngine ingest contract together, and
each one is a SHA in one repo naming a commit in the other:

* RegEngine's ``.github/workflows/inflow-contract-ci.yml`` pins
  ``INFLOW_LAB_REF``. That is the simulator commit RegEngine's contract
  test actually exercises. It was the subject of issue #104: it sat far
  enough behind inflow-lab main that the controller-side verdict-pairing
  rewrite (#85, ``_pair_event_responses``) had never run against a real
  RegEngine.
* This repo pins the RegEngine ref its own cross-repo job checks out.
  That one defaults to tracking RegEngine's default branch, so it only
  becomes a pin when a maintainer sets the ``REGENGINE_CONTRACT_REF``
  repository variable to work around an upstream outage -- and this check
  is what stops that workaround from quietly becoming permanent.

A raw commit count is only a proxy for risk. What actually matters is
whether anything on the *contract surface* changed since the pin, so a
single surface commit is a breach regardless of how few commits there
are. The commit count stays as a backstop for churn that touches the
surface indirectly.

The GitHub compare API answers both questions in one call:

    gh api "repos/OWNER/REPO/compare/BASE...HEAD" > comparison.json
    python scripts/contract_pin_drift.py --comparison comparison.json ...

so this module only has to decide, format and exit -- which keeps the
decision unit-testable without a network (tests/test_contract_provenance.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# GitHub's compare endpoint caps what it will return in one response: at
# most 250 commits and at most 300 files. Past either cap the payload is
# silently partial, so "no surface file in the list" stops meaning "no
# surface file changed".
COMPARE_COMMIT_LIMIT = 250
COMPARE_FILE_LIMIT = 300

# The files whose change makes a pin stale, per repo. These live here
# rather than as flags in the workflow so they are covered by tests and so
# both directions of the check read from one list.
#
# The RegEngine side is copied from the path filter RegEngine's own
# .github/workflows/inflow-contract-ci.yml uses to decide whether a change
# is contract-impacting -- i.e. upstream's own declaration of its contract
# surface, not a guess made here.
CONTRACT_SURFACE_PRESETS: dict[str, tuple[str, ...]] = {
    "inflow-lab": (
        "app/regengine_client.py",
        "app/controller.py",
        "app/contract.py",
        "app/cte_rules.py",
        "app/mock_service.py",
        "app/schemas/*",
        # RegEngine's contract job runs this script; changing it changes
        # what the pinned commit actually exercises.
        "scripts/live_trial.py",
    ),
    "regengine": (
        "alembic/*",
        "requirements.in",
        "requirements.lock",
        "services/ingestion/app/authz.py",
        "services/ingestion/app/config.py",
        "services/ingestion/app/tenant_validation.py",
        "services/ingestion/app/webhook_compat.py",
        "services/ingestion/app/webhook_models.py",
        "services/ingestion/app/webhook_router_v2/*",
        "services/ingestion/main.py",
        "services/shared/auth.py",
        "services/shared/canonical_event.py",
        "services/shared/canonical_persistence/*",
        "services/shared/cte_persistence/*",
        "services/shared/funnel_events.py",
        "services/shared/idempotency.py",
    ),
}

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_BAD_INPUT = 3


class DriftInputError(RuntimeError):
    """The comparison payload was not shaped like a GitHub compare response."""


@dataclass(frozen=True, slots=True)
class DriftReport:
    ahead_by: int
    max_commits: int
    surface_files: tuple[str, ...]
    truncated: bool
    reasons: tuple[str, ...] = field(default=())

    @property
    def breached(self) -> bool:
        return bool(self.reasons)


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DriftInputError(f"compare response field {name!r} was {value!r}, expected an integer.")
    return value


def changed_files(comparison: Mapping[str, Any]) -> tuple[str, ...]:
    files = comparison.get("files") or []
    if not isinstance(files, list):
        raise DriftInputError("compare response field 'files' was not a list.")
    names: list[str] = []
    for entry in files:
        if isinstance(entry, Mapping) and isinstance(entry.get("filename"), str):
            names.append(entry["filename"])
    return tuple(names)


def is_truncated(comparison: Mapping[str, Any]) -> bool:
    """True when the compare payload may be missing files we care about."""
    total_commits = _as_int(comparison.get("total_commits", 0), "total_commits")
    commits = comparison.get("commits")
    listed_commits = len(commits) if isinstance(commits, list) else 0
    if total_commits > listed_commits:
        return True
    if total_commits >= COMPARE_COMMIT_LIMIT:
        return True
    return len(changed_files(comparison)) >= COMPARE_FILE_LIMIT


def matching_surface_files(filenames: Iterable[str], patterns: Sequence[str]) -> tuple[str, ...]:
    matched = {name for name in filenames if any(fnmatchcase(name, pattern) for pattern in patterns)}
    return tuple(sorted(matched))


def evaluate_drift(
    comparison: Mapping[str, Any],
    *,
    max_commits: int,
    surface_patterns: Sequence[str],
) -> DriftReport:
    """Turn a compare response into a pass/breach decision plus its reasons."""
    if not isinstance(comparison, Mapping):
        raise DriftInputError("compare response was not a JSON object.")
    if "ahead_by" not in comparison:
        raise DriftInputError("compare response has no 'ahead_by' field.")

    ahead_by = _as_int(comparison["ahead_by"], "ahead_by")
    truncated = is_truncated(comparison)
    surface_files = matching_surface_files(changed_files(comparison), surface_patterns)

    reasons: list[str] = []
    if ahead_by > max_commits:
        reasons.append(f"the pin is {ahead_by} commits behind (threshold {max_commits})")
    if surface_files:
        reasons.append(
            "contract-surface files changed since the pin, so the pinned commit no longer "
            f"exercises the contract as it exists: {', '.join(surface_files)}"
        )
    if truncated and not surface_files:
        # Only worth saying when it changes the answer; when a surface file
        # already breached, the truncation adds nothing.
        reasons.append(
            "the compare response was truncated, so whether contract-surface files changed "
            "cannot be determined from it -- treated as drifted rather than assumed clean"
        )
    return DriftReport(
        ahead_by=ahead_by,
        max_commits=max_commits,
        surface_files=surface_files,
        truncated=truncated,
        reasons=tuple(reasons),
    )


PINNED_REF_PATTERN = "^[ \t]*{key}:[ \t]*[\"']?([0-9a-fA-F]{{7,40}})[\"']?[ \t]*(?:#.*)?$"


def extract_pinned_ref(workflow_text: str, key: str = "INFLOW_LAB_REF") -> str:
    """Pull ``key: <sha>`` out of a workflow file.

    Deliberately a line regex rather than a YAML parse: this repo depends
    on no YAML library of its own (the one in the lock is transitive
    through uvicorn's extras and could disappear on any lock refresh), and
    the value lives on a single ``env:`` line upstream.

    Raises rather than returning a default when the key is absent, because
    "RegEngine restructured the workflow that pins us" is itself news the
    drift check exists to deliver -- not a condition to silently pass.
    """
    match = re.search(PINNED_REF_PATTERN.format(key=re.escape(key)), workflow_text, re.MULTILINE)
    if not match:
        raise DriftInputError(
            f"Could not find a `{key}: <sha>` line in the workflow. If upstream moved or "
            "renamed the pin, this checker needs updating -- do not assume it is fine."
        )
    return match.group(1)


def render_summary(report: DriftReport, *, label: str, base_ref: str, head_ref: str, blocking: bool) -> str:
    verdict = "OK" if not report.breached else ("FAILED" if blocking else "WARNING")
    lines = [
        f"### Contract pin drift: {label} -- {verdict}",
        "",
        f"- Pinned commit: `{base_ref}`",
        f"- Compared against: `{head_ref}`",
        f"- Commits behind: **{report.ahead_by}** (threshold {report.max_commits})",
        f"- Contract-surface files changed since the pin: **{len(report.surface_files)}**",
    ]
    for name in report.surface_files:
        lines.append(f"  - `{name}`")
    if report.truncated:
        lines.append("- Compare response was truncated by the GitHub API.")
    for reason in report.reasons:
        lines.append(f"- {verdict.title()} reason: {reason}")
    lines.append("")
    return "\n".join(lines)


def _write_step_summary(text: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _load_comparison(path: str) -> Mapping[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise DriftInputError(f"compare response from {path!r} was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DriftInputError(f"compare response from {path!r} was not a JSON object.")
    return payload


def _cmd_pinned_ref(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.workflow == "-" else Path(args.workflow).read_text(encoding="utf-8")
    print(extract_pinned_ref(text, args.key))
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    blocking = args.on_breach == "fail"
    patterns = tuple(CONTRACT_SURFACE_PRESETS.get(args.surface_preset, ())) + tuple(args.surface_paths)
    comparison = _load_comparison(args.comparison)
    report = evaluate_drift(comparison, max_commits=args.max_commits, surface_patterns=patterns)

    summary = render_summary(
        report,
        label=args.label,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        blocking=blocking,
    )
    _write_step_summary(summary)
    print(summary)

    if not report.breached:
        print(f"::notice::{args.label} is within the drift threshold ({report.ahead_by} commits behind).")
        return EXIT_OK

    level = "error" if blocking else "warning"
    for reason in report.reasons:
        print(f"::{level}::{args.label}: {reason}")
    if not blocking:
        print(
            f"::warning::{args.label} can only be advanced in the other repository, so this run "
            "reports it instead of blocking; the scheduled run does enforce it."
        )
    return EXIT_BREACH if blocking else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail when a cross-repo contract pin has drifted too far.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pinned = subparsers.add_parser("pinned-ref", help="Print the SHA a workflow file pins.")
    pinned.add_argument("--workflow", required=True, help="Workflow file to read ('-' for stdin).")
    pinned.add_argument("--key", default="INFLOW_LAB_REF", help="Env key holding the pin.")
    pinned.set_defaults(func=_cmd_pinned_ref)

    check = subparsers.add_parser("check", help="Decide whether a pin has drifted too far.")
    check.add_argument("--comparison", required=True, help="GitHub compare API JSON ('-' for stdin).")
    check.add_argument("--label", required=True, help="Human name of the pin being checked.")
    check.add_argument("--base-ref", default="(pinned ref)", help="The pinned commit, for messages.")
    check.add_argument("--head-ref", default="(default branch)", help="What it was compared to.")
    check.add_argument("--max-commits", type=int, required=True, help="Commit-count threshold N.")
    check.add_argument(
        "--surface-preset",
        choices=sorted(CONTRACT_SURFACE_PRESETS),
        help="Which repo's contract surface to look for in the diff.",
    )
    check.add_argument(
        "--surface-path",
        action="append",
        default=[],
        dest="surface_paths",
        help="Extra contract-surface path or fnmatch pattern; repeatable. Any match is a breach.",
    )
    check.add_argument(
        "--on-breach",
        choices=("fail", "warn"),
        default="fail",
        help=(
            "fail: exit non-zero (use where this repo can act on the drift). "
            "warn: annotate and exit 0 (use where only the other repo can)."
        ),
    )
    check.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DriftInputError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except OSError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    raise SystemExit(main())

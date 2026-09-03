"""Fail when the RegEngine ingest contract pin goes stale or drifts.

Two independent gaps this closes, both of which let the "green demo, failing
live post" drift come back silently:

#140 -- staleness. ``tests/test_regengine_contract_pin.py`` compares two dicts
that both live in THIS repo, so it can only catch an inflow-lab-side edit. If
RegEngine changes its required-KDE table and nobody hand-copies it here, the
pin and ``app/cte_rules.py`` keep agreeing with each other while both disagree
with production. There is no upstream fetch available from a plain unit test,
so the guard is a dated re-confirmation: someone has to look at RegEngine's
table every ``MAX_PIN_AGE_DAYS`` and move ``PIN_CONFIRMED_ON`` forward.

#104 -- pin drift. The only cross-repo contract test lives in RegEngine and
checks inflow-lab out at a hardcoded ``INFLOW_LAB_REF``. Nothing advances it
and nothing fails when it falls behind, so every wire-facing change merged
here since that SHA shipped to the shared demo having never run against a real
RegEngine. Given a RegEngine checkout this compares the pinned SHA against
inflow-lab HEAD and fails past ``MAX_PIN_DRIFT_COMMITS``.

Usage:
    python scripts/contract_pin_check.py                     # staleness only
    python scripts/contract_pin_check.py --regengine-path DIR  # + cross-repo

Exit code 0 = pass, 1 = a guard tripped, 2 = the check could not run.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess  # nosec B404 - one fixed-argv, read-only `git rev-list`; see commits_ahead_of
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # So `python scripts/contract_pin_check.py` works from a clean checkout.
    sys.path.insert(0, str(REPO_ROOT))

#: Last time a human compared this repo's required-KDE table against
#: RegEngine's ``REQUIRED_KDES_BY_CTE``. Move this forward ONLY after actually
#: re-reading RegEngine's table -- moving it to silence a failure is the exact
#: failure mode this guard exists to prevent.
PIN_CONFIRMED_ON = date(2026, 8, 26)

#: How long a re-confirmation stays good. One quarter: long enough not to be
#: busywork, short enough that a contract change cannot sit undetected across
#: two release cycles.
MAX_PIN_AGE_DAYS = 90

#: How far RegEngine's INFLOW_LAB_REF may fall behind inflow-lab main before
#: the cross-repo contract test is testing a simulator nobody is running.
MAX_PIN_DRIFT_COMMITS = 5

#: The skill reference operators and agents read before touching the wire. A
#: confidently wrong sentence in here kept #91 alive for months, so the
#: documented per-event field set is checked against what the client emits.
CONTRACT_REFERENCE = Path(".agents/skills/regengine-api-contract/references/contract.md")

#: Where each side keeps its half of the contract.
REGENGINE_WORKFLOW = Path(".github/workflows/inflow-contract-ci.yml")
REGENGINE_WEBHOOK_MODELS = Path("services/ingestion/app/webhook_models.py")

#: Top-level field RegEngine's ingest path reads input lot codes from. It reads
#: them from NOWHERE else -- not from ``kdes``.
INPUT_LOT_CODES_FIELD = "input_traceability_lot_codes"

REF_PATTERN = re.compile(r"^\s*INFLOW_LAB_REF:\s*['\"]?([0-9a-fA-F]{7,40})['\"]?\s*$", re.MULTILINE)


class ContractPinFailure(RuntimeError):
    """A guard tripped, or the check could not be carried out."""


# ---------------------------------------------------------------------------
# #140 -- staleness
# ---------------------------------------------------------------------------


def pin_age_days(today: date | None = None) -> int:
    return ((today or date.today()) - PIN_CONFIRMED_ON).days


def check_pin_freshness(today: date | None = None) -> str:
    age = pin_age_days(today)
    if age > MAX_PIN_AGE_DAYS:
        raise ContractPinFailure(
            f"The RegEngine required-KDE pin was last confirmed {age} days ago "
            f"({PIN_CONFIRMED_ON.isoformat()}), past the {MAX_PIN_AGE_DAYS}-day "
            "window. Re-read REQUIRED_KDES_BY_CTE in RegEngine's "
            f"{REGENGINE_WEBHOOK_MODELS}, reconcile it with app/cte_rules.py and "
            "tests/test_regengine_contract_pin.py, then move PIN_CONFIRMED_ON in "
            "scripts/contract_pin_check.py to today. Do not move the date without "
            "doing the comparison -- that reinstates the silent drift."
        )
    return f"pin confirmed {age} days ago (window {MAX_PIN_AGE_DAYS} days)"


# ---------------------------------------------------------------------------
# #104 -- cross-repo pin drift
# ---------------------------------------------------------------------------


def pinned_inflow_lab_ref(regengine_path: Path) -> str:
    workflow = regengine_path / REGENGINE_WORKFLOW
    if not workflow.is_file():
        raise ContractPinFailure(f"No RegEngine contract workflow at {workflow}")
    match = REF_PATTERN.search(workflow.read_text(encoding="utf-8"))
    if not match:
        raise ContractPinFailure(f"No INFLOW_LAB_REF found in {workflow}")
    return match.group(1)


def commits_ahead_of(ref: str) -> int:
    """How many commits inflow-lab HEAD is past ``ref``.

    Read-only. Needs full history, so CI must check out with fetch-depth: 0.
    """
    try:
        # nosec below waives bandit B603/B607: fixed argv, no shell, and the
        # `git` on PATH is the same one the surrounding CI job used to check
        # this repo out. `ref` is a hex SHA parsed out of RegEngine's workflow
        # by REF_PATTERN, so it cannot carry argv-shaped input.
        completed = subprocess.run(  # nosec
            ["git", "rev-list", "--count", f"{ref}..HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractPinFailure(
            f"Could not measure drift from {ref}: {exc}. The pinned commit must "
            "exist in this checkout -- fetch with depth 0."
        ) from exc
    return int(completed.stdout.strip() or 0)


def check_pin_drift(regengine_path: Path) -> str:
    ref = pinned_inflow_lab_ref(regengine_path)
    behind = commits_ahead_of(ref)
    if behind > MAX_PIN_DRIFT_COMMITS:
        raise ContractPinFailure(
            f"RegEngine pins INFLOW_LAB_REF={ref}, which inflow-lab main is "
            f"{behind} commits past (limit {MAX_PIN_DRIFT_COMMITS}). The only "
            "cross-repo ingest contract test is therefore running against a "
            "simulator nobody is deploying. Advance INFLOW_LAB_REF in RegEngine's "
            f"{REGENGINE_WORKFLOW} (or point it at main)."
        )
    return f"INFLOW_LAB_REF={ref} is {behind} commit(s) behind (limit {MAX_PIN_DRIFT_COMMITS})"


# ---------------------------------------------------------------------------
# #104 -- required-KDE table, sourced from RegEngine rather than hand-copied
# ---------------------------------------------------------------------------


def _key_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr.upper()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.upper().replace("-", "_")
    return None


def _string_sequence(node: ast.expr) -> tuple[str, ...] | None:
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    values = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        values.append(element.value)
    return tuple(values)


def regengine_required_kdes(regengine_path: Path) -> dict[str, tuple[str, ...]]:
    """Parse ``REQUIRED_KDES_BY_CTE`` out of RegEngine without importing it.

    AST rather than import because RegEngine's module pulls in its own
    dependency tree, and keyed by upper-cased member/label name so it compares
    cleanly whether RegEngine keys the table by enum member or by string.
    """
    source_path = regengine_path / REGENGINE_WEBHOOK_MODELS
    if not source_path.is_file():
        raise ContractPinFailure(f"No RegEngine webhook models at {source_path}")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    for node in ast.walk(tree):
        targets: list[Any] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REQUIRED_KDES_BY_CTE" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        table: dict[str, tuple[str, ...]] = {}
        for key, value in zip(node.value.keys, node.value.values):
            name = _key_name(key) if key is not None else None
            kdes = _string_sequence(value)
            if name is None or kdes is None:
                raise ContractPinFailure(
                    "REQUIRED_KDES_BY_CTE in RegEngine is no longer a literal "
                    "mapping of CTE -> tuple of KDE names; this parser needs "
                    "updating before it can be trusted."
                )
            table[name] = kdes
        return table
    raise ContractPinFailure(f"No REQUIRED_KDES_BY_CTE assignment found in {source_path}")


def local_required_kdes() -> dict[str, tuple[str, ...]]:
    from app.cte_rules import REQUIRED_KDES

    return {cte.name.upper(): tuple(kdes) for cte, kdes in REQUIRED_KDES.items()}


def check_required_kdes(regengine_path: Path) -> str:
    upstream = regengine_required_kdes(regengine_path)
    local = local_required_kdes()
    if upstream == local:
        return f"required-KDE table matches RegEngine for all {len(local)} CTE types"

    differences = []
    for name in sorted(set(upstream) | set(local)):
        if upstream.get(name) != local.get(name):
            differences.append(
                f"  {name}: regengine={upstream.get(name)!r} inflow-lab={local.get(name)!r}"
            )
    raise ContractPinFailure(
        "app/cte_rules.REQUIRED_KDES disagrees with RegEngine's "
        "REQUIRED_KDES_BY_CTE:\n" + "\n".join(differences)
    )


# ---------------------------------------------------------------------------
# The documented per-event wire shape vs. what the client actually sends
# ---------------------------------------------------------------------------


SAMPLE_EVENT: dict[str, Any] = {
    "cte_type": "transformation",
    "traceability_lot_code": "TLC-20260427-000001",
    "product_description": "Chopped Romaine",
    "quantity": 500,
    "unit_of_measure": "cases",
    "location_name": "Valley Fresh Processing",
    "timestamp": "2026-04-27T08:30:00Z",
    "kdes": {},
}


def documented_event_fields() -> set[str]:
    """Top-level keys of the per-event JSON sample in the skill reference."""
    source_path = REPO_ROOT / CONTRACT_REFERENCE
    if not source_path.is_file():
        raise ContractPinFailure(f"No contract reference at {source_path}")
    text = source_path.read_text(encoding="utf-8")
    heading = text.find("## Per-event payload fields")
    if heading < 0:
        raise ContractPinFailure(
            f"{CONTRACT_REFERENCE} no longer has a '## Per-event payload fields' section"
        )
    block = re.search(r"```json\n(.*?)```", text[heading:], re.DOTALL)
    if not block:
        raise ContractPinFailure(
            f"No JSON sample under 'Per-event payload fields' in {CONTRACT_REFERENCE}"
        )
    try:
        sample = json.loads(block.group(1))
    except ValueError as exc:
        raise ContractPinFailure(
            f"The per-event JSON sample in {CONTRACT_REFERENCE} does not parse: {exc}"
        ) from exc
    if not isinstance(sample, dict):
        raise ContractPinFailure(f"The per-event sample in {CONTRACT_REFERENCE} is not an object")
    return set(sample)


def emitted_event_fields(*, with_input_lots: bool) -> set[str]:
    from app.regengine_client import build_wire_body
    from app.schemas.ingestion import IngestPayload

    event = dict(SAMPLE_EVENT)
    if with_input_lots:
        event["kdes"] = {"input_traceability_lot_codes": ["TLC-20260427-000002"]}
    payload = IngestPayload.model_validate({"source": "contract-pin-check", "events": [event]})
    return set(build_wire_body(payload)["events"][0])


def check_documented_wire_fields() -> str:
    """Keep the reference's field list honest about what goes on the wire.

    #91: the reference claimed RegEngine read input lot codes out of ``kdes``,
    so the simulator sent them only there and every transformation lineage link
    was dropped on live ingest. Documenting the wire shape is only useful if
    something fails when the document and the client disagree.
    """
    documented = documented_event_fields()
    baseline = emitted_event_fields(with_input_lots=False)
    if documented != baseline:
        missing = sorted(baseline - documented)
        extra = sorted(documented - baseline)
        raise ContractPinFailure(
            f"The per-event sample in {CONTRACT_REFERENCE} does not match what "
            f"app.regengine_client.build_wire_body emits: undocumented "
            f"{missing or 'none'}, documented but not sent {extra or 'none'}."
        )

    with_lots = emitted_event_fields(with_input_lots=True)
    added = with_lots - baseline
    if added != {INPUT_LOT_CODES_FIELD}:
        raise ContractPinFailure(
            "An event declaring input lot codes must add exactly "
            f"{INPUT_LOT_CODES_FIELD!r} at the TOP LEVEL -- RegEngine's ingest "
            f"path reads it nowhere else -- but build_wire_body added {sorted(added)}."
        )
    return (
        f"{len(documented)} documented per-event fields match build_wire_body, "
        f"and input lots are hoisted to top-level {INPUT_LOT_CODES_FIELD!r}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regengine-path",
        type=Path,
        default=None,
        help="Path to a RegEngine checkout. Without it only the staleness guard runs.",
    )
    args = parser.parse_args(argv)

    checks: list[tuple[str, Any]] = [
        ("pin freshness", lambda: check_pin_freshness()),
        ("documented wire fields", check_documented_wire_fields),
    ]
    if args.regengine_path is not None:
        checks.append(("pin drift", lambda: check_pin_drift(args.regengine_path)))
        checks.append(("required KDEs", lambda: check_required_kdes(args.regengine_path)))
    else:
        print(
            "note: no --regengine-path given, so pin drift and the upstream "
            "required-KDE comparison were NOT checked."
        )

    failures = 0
    for label, check in checks:
        try:
            print(f"  ok    {label}: {check()}")
        except ContractPinFailure as exc:
            print(f"  FAIL  {label}: {exc}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"\nContract pin check failed ({failures} problem(s)).", file=sys.stderr)
        return 1
    print("\nContract pin check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read RegEngine's required-KDE-by-CTE table straight out of its source.

RegEngine's source of truth for live ingest validation is the Python
constant ``REQUIRED_KDES_BY_CTE`` in
``services/ingestion/app/webhook_models.py``. It is not served by any
endpoint (``/health`` advertises only ``inflow_contract_version``), so the
only way to compare against the real table is to read that file.

This module does that by parsing the file with ``ast`` rather than
importing it: importing would drag in RegEngine's pydantic models and its
``shared.*`` package, which this repo does not and should not install.
Parsing keeps the check runnable anywhere a RegEngine checkout exists --
including the cross-repo CI job, which checks RegEngine out next to this
repo.

Three commands, all of which operate on a RegEngine checkout:

``extract``
    Print/write the upstream table as flat ``{cte_type: [kde, ...]}``
    JSON. That is exactly the shape ``app.contract.check_remote_kde_contract``
    consumes via ``REGENGINE_CONTRACT_ARTIFACT_PATH``, so CI pipes this
    into ``python -m app.contract`` to diff it against the *running*
    table, ``app.cte_rules.REQUIRED_KDES``.

``snapshot``
    Regenerate ``tests/data/regengine_required_kdes.json`` -- the vendored
    copy the offline unit test (``tests/test_regengine_contract_pin.py``)
    compares against, carrying machine-checkable provenance: which repo,
    file and symbol it came from, at which commit, and a sha256 over the
    table itself.

``verify``
    Fail if the vendored snapshot no longer equals the upstream table, or
    if the snapshot's recorded sha256 no longer matches its own contents
    (i.e. somebody hand-edited the JSON instead of regenerating it). This
    is the command the cross-repo CI job runs; it is what makes the
    vendored copy a *checked* mirror rather than a claim in a comment.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]

# Where RegEngine keeps the table, relative to a RegEngine checkout root.
REGENGINE_SOURCE_PATH = "services/ingestion/app/webhook_models.py"
REGENGINE_TABLE_SYMBOL = "REQUIRED_KDES_BY_CTE"
REGENGINE_ENUM_SYMBOL = "WebhookCTEType"
REGENGINE_REPOSITORY = "regengine/regengine"

DEFAULT_SNAPSHOT_PATH = REPO_ROOT / "tests" / "data" / "regengine_required_kdes.json"

# Exit codes, kept distinct so a workflow step (or an operator) can tell
# "the tables disagree" from "I could not read the upstream table at all".
EXIT_OK = 0
EXIT_MISMATCH = 2
EXIT_UNREADABLE = 3


class ContractSourceError(RuntimeError):
    """RegEngine's source could not be read in the shape we expect.

    Raised rather than returning a partial table on purpose: a table that
    silently lost a CTE type because upstream refactored the constant is
    indistinguishable, downstream, from a table that genuinely shrank.
    Both must fail loudly.
    """


@dataclass(frozen=True, slots=True)
class KdeSnapshot:
    """The vendored copy of RegEngine's table, plus where it came from."""

    table: dict[str, tuple[str, ...]]
    provenance: dict[str, Any]

    @property
    def recorded_table_sha256(self) -> str:
        return str(self.provenance.get("table_sha256", ""))


def table_sha256(table: Mapping[str, Sequence[str]]) -> str:
    """Checksum over the table alone, order-normalized.

    Keys are sorted and KDE lists are left in upstream order, so the digest
    changes when the contract changes and not when a JSON formatter moves
    whitespace around.
    """
    canonical = json.dumps(
        {key: list(value) for key, value in sorted(table.items())},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _enum_members(tree: ast.Module, enum_name: str) -> dict[str, str]:
    """Map ``EnumName.MEMBER`` -> its string value."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != enum_name:
            continue
        members: dict[str, str] = {}
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            value = statement.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
                members[target.id] = value.value
        if not members:
            raise ContractSourceError(f"{enum_name} defines no string members.")
        return members
    raise ContractSourceError(f"Could not find `class {enum_name}` in RegEngine's source.")


def _table_node(tree: ast.Module, symbol: str) -> ast.Dict:
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            # `REQUIRED_KDES_BY_CTE: Dict[...] = {...}` -- an annotated
            # assignment, which is what upstream actually writes today.
            targets = [node.target]
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == symbol for target in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            raise ContractSourceError(f"{symbol} is not a dict literal in RegEngine's source.")
        return node.value
    raise ContractSourceError(f"Could not find `{symbol}` at module level in RegEngine's source.")


def extract_required_kdes(source: str) -> dict[str, tuple[str, ...]]:
    """Parse RegEngine's ``webhook_models.py`` text into ``{cte: (kde, ...)}``."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - upstream would be broken
        raise ContractSourceError(f"RegEngine's source did not parse: {exc}") from exc

    members = _enum_members(tree, REGENGINE_ENUM_SYMBOL)
    table_node = _table_node(tree, REGENGINE_TABLE_SYMBOL)

    table: dict[str, tuple[str, ...]] = {}
    for key_node, value_node in zip(table_node.keys, table_node.values):
        if not isinstance(key_node, ast.Attribute) or not isinstance(key_node.value, ast.Name):
            raise ContractSourceError(
                f"{REGENGINE_TABLE_SYMBOL} has a key that is not "
                f"{REGENGINE_ENUM_SYMBOL}.MEMBER; cannot resolve it without executing upstream code."
            )
        if key_node.value.id != REGENGINE_ENUM_SYMBOL or key_node.attr not in members:
            raise ContractSourceError(
                f"{REGENGINE_TABLE_SYMBOL} key {ast.unparse(key_node)} is not a known "
                f"{REGENGINE_ENUM_SYMBOL} member."
            )
        if not isinstance(value_node, (ast.List, ast.Tuple)):
            raise ContractSourceError(
                f"{REGENGINE_TABLE_SYMBOL}[{ast.unparse(key_node)}] is not a list/tuple literal."
            )
        kdes: list[str] = []
        for element in value_node.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                raise ContractSourceError(
                    f"{REGENGINE_TABLE_SYMBOL}[{ast.unparse(key_node)}] contains a non-string entry."
                )
            kdes.append(element.value)
        table[members[key_node.attr]] = tuple(kdes)

    if not table:
        raise ContractSourceError(f"{REGENGINE_TABLE_SYMBOL} parsed as empty.")
    return table


def regengine_source_path(regengine_root: Path) -> Path:
    return Path(regengine_root) / REGENGINE_SOURCE_PATH


def extract_from_checkout(regengine_root: Path) -> dict[str, tuple[str, ...]]:
    path = regengine_source_path(regengine_root)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractSourceError(f"Could not read {path}: {exc}") from exc
    return extract_required_kdes(source)


def load_snapshot(path: Path | str = DEFAULT_SNAPSHOT_PATH) -> KdeSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "required_kdes_by_cte" not in raw:
        raise ContractSourceError(f"{path} is not a KDE snapshot document.")
    table = {str(key): tuple(value) for key, value in raw["required_kdes_by_cte"].items()}
    provenance = raw.get("provenance") or {}
    return KdeSnapshot(table=table, provenance=dict(provenance))


def build_snapshot_document(
    table: Mapping[str, Sequence[str]],
    *,
    source_commit: str | None,
    extracted_at: str | None = None,
) -> dict[str, Any]:
    ordered = {key: list(value) for key, value in sorted(table.items())}
    return {
        "provenance": {
            "source_repository": REGENGINE_REPOSITORY,
            "source_path": REGENGINE_SOURCE_PATH,
            "source_symbol": REGENGINE_TABLE_SYMBOL,
            "source_enum": REGENGINE_ENUM_SYMBOL,
            "source_commit": source_commit or "unknown",
            "extracted_by": "scripts/regengine_kde_contract.py",
            "extracted_at": extracted_at or datetime.now(UTC).date().isoformat(),
            "table_sha256": table_sha256(ordered),
            "regenerate": (
                "python scripts/regengine_kde_contract.py snapshot "
                "--regengine-root <regengine checkout> --source-commit <sha>"
            ),
            "enforced_by": (
                "tests/test_contract_provenance.py (checksum + shape, offline) and the "
                "regengine-contract job in .github/workflows/ci.yml, which re-extracts "
                "this table from a real RegEngine checkout and fails on any difference."
            ),
        },
        "required_kdes_by_cte": ordered,
    }


def _write_json(document: Any, output: str | None) -> None:
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _describe_difference(
    snapshot_table: Mapping[str, Sequence[str]],
    upstream_table: Mapping[str, Sequence[str]],
) -> list[str]:
    problems: list[str] = []
    for cte_type in sorted(set(upstream_table) - set(snapshot_table)):
        problems.append(f"missing CTE type {cte_type!r}: upstream requires {list(upstream_table[cte_type])}")
    for cte_type in sorted(set(snapshot_table) - set(upstream_table)):
        problems.append(f"unknown CTE type {cte_type!r}: upstream has no such entry")
    for cte_type in sorted(set(snapshot_table) & set(upstream_table)):
        local = list(snapshot_table[cte_type])
        upstream = list(upstream_table[cte_type])
        if local != upstream:
            problems.append(f"{cte_type}: snapshot {local} != upstream {upstream}")
    return problems


def _cmd_extract(args: argparse.Namespace) -> int:
    table = extract_from_checkout(Path(args.regengine_root))
    _write_json({key: list(value) for key, value in sorted(table.items())}, args.output)
    return EXIT_OK


def _cmd_snapshot(args: argparse.Namespace) -> int:
    table = extract_from_checkout(Path(args.regengine_root))
    document = build_snapshot_document(table, source_commit=args.source_commit)
    _write_json(document, args.output or str(DEFAULT_SNAPSHOT_PATH))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    snapshot = load_snapshot(args.snapshot)
    upstream = extract_from_checkout(Path(args.regengine_root))

    recomputed = table_sha256(snapshot.table)
    if snapshot.recorded_table_sha256 != recomputed:
        print(
            "::error::The vendored RegEngine KDE snapshot was edited by hand: its recorded "
            f"table_sha256 ({snapshot.recorded_table_sha256 or 'missing'}) does not match its "
            f"contents ({recomputed}). Regenerate it with "
            "`python scripts/regengine_kde_contract.py snapshot --regengine-root <checkout>`.",
            file=sys.stderr,
        )
        return EXIT_MISMATCH

    problems = _describe_difference(snapshot.table, upstream)
    if problems:
        for problem in problems:
            print(f"::error::RegEngine KDE contract drift -- {problem}", file=sys.stderr)
        print(
            "::error::Update app/cte_rules.py REQUIRED_KDES and regenerate "
            f"{args.snapshot} from RegEngine, then bump INFLOW_CONTRACT_VERSION on both sides.",
            file=sys.stderr,
        )
        return EXIT_MISMATCH

    print(
        f"RegEngine required-KDE table matches the vendored snapshot "
        f"({len(upstream)} CTE types, sha256 {recomputed})."
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_root(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--regengine-root",
            required=True,
            help="Path to a RegEngine checkout (the directory holding services/).",
        )

    extract = subparsers.add_parser("extract", help="Print upstream {cte_type: [kde, ...]} JSON.")
    add_root(extract)
    extract.add_argument("--output", help="Write to this path instead of stdout.")
    extract.set_defaults(func=_cmd_extract)

    snapshot = subparsers.add_parser("snapshot", help="Regenerate the vendored snapshot file.")
    add_root(snapshot)
    snapshot.add_argument("--source-commit", help="RegEngine commit the table was read from.")
    snapshot.add_argument("--output", help=f"Defaults to {DEFAULT_SNAPSHOT_PATH}.")
    snapshot.set_defaults(func=_cmd_snapshot)

    verify = subparsers.add_parser("verify", help="Fail if the vendored snapshot has drifted.")
    add_root(verify)
    verify.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT_PATH))
    verify.set_defaults(func=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ContractSourceError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    except OSError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":
    raise SystemExit(main())

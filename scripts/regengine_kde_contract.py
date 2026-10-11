"""Read RegEngine's required-KDE-by-CTE table straight out of its source.

RegEngine's source of truth for live ingest validation is the Python
constant ``REQUIRED_KDES_BY_CTE`` in
``services/ingestion/app/webhook_models.py``. It is not served by any
endpoint (``/health`` advertises only ``inflow_contract_version``), so the
only way to compare against the real table is to read that file.

This module does that by parsing the file with ``ast`` rather than
importing it: importing would drag in RegEngine's pydantic models and its
``shared.*`` package, which this repo does not and should not install.
Parsing keeps the local maintenance commands runnable anywhere a RegEngine
checkout exists. The trusted cross-repo workflow does not check out or run
RegEngine: it resolves an immutable commit, fetches only
``webhook_models.py`` into the expected source path, and parses that file.

Three commands cover local snapshot maintenance and hosted verification:

``extract``
    Print/write the upstream table as flat ``{cte_type: [kde, ...]}``
    JSON. That is exactly the shape ``app.contract.check_remote_kde_contract``
    consumes via ``REGENGINE_CONTRACT_ARTIFACT_PATH``. This command is a
    local checkout utility; the hosted gate uses ``verify`` directly.

``snapshot``
    Regenerate ``tests/data/regengine_required_kdes.json`` -- the vendored
    copy the offline unit test (``tests/test_regengine_contract_pin.py``)
    compares against, carrying machine-checkable provenance: which repo,
    file and symbol it came from, at which commit, a sha256 over the exact
    source bytes, and a sha256 over the table itself.

``verify``
    Fail if the fetched upstream source bytes do not match the snapshot's
    recorded source sha256, before parsing any Python. After that provenance
    check, fail if the vendored snapshot no longer equals the upstream table,
    or if the snapshot's recorded table sha256 no longer matches its own
    contents (i.e. somebody hand-edited the JSON instead of regenerating it).
    This also compares both repositories' literal ``INFLOW_CONTRACT_VERSION``.
    Its root needs only the single expected upstream source file, so the
    trusted contract-pin workflow runs it against ``webhook_models.py``
    fetched at a resolved immutable RegEngine commit. That makes the vendored
    copy a *checked* mirror rather than a claim in a comment, without importing
    or executing upstream code.
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
CONTRACT_VERSION_SYMBOL = "INFLOW_CONTRACT_VERSION"
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

    @property
    def recorded_source_sha256(self) -> str:
        return str(self.provenance.get("source_sha256", ""))


def source_bytes_sha256(source: bytes) -> str:
    """Checksum over the exact fetched upstream source bytes."""
    return hashlib.sha256(source).hexdigest()


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


_DYNAMIC_NAMESPACE_NAMES = frozenset(
    {
        "__builtins__",
        "__annotations__",
        "__delattr__",
        "__getattribute__",
        "__import__",
        "__setattr__",
        "attrgetter",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "locals",
        "methodcaller",
        "setattr",
        "vars",
    }
)

_DYNAMIC_NAMESPACE_ATTRIBUTES = frozenset(
    {
        "__dict__",
        "__annotations__",
        "__delattr__",
        "__getattribute__",
        "__import__",
        "__setattr__",
        "__globals__",
        "attrgetter",
        "delattr",
        "eval",
        "exec",
        "f_globals",
        "f_locals",
        "getattr",
        "getattr_static",
        "getmodule",
        "globals",
        "locals",
        "methodcaller",
        "setattr",
        "vars",
    }
)

_ALLOWED_CONTRACT_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "cte_rules",
        "dataclasses",
        "datetime",
        "enum",
        "httpx",
        "json",
        "logging",
        "os",
        "pathlib",
        "pydantic",
        "re",
        "shared",
        "sys",
        "typing",
        "uuid",
    }
)

_REFLECTIVE_STRING_MARKERS = (
    "__builtins__",
    "__annotations__",
    "__dict__",
    "__globals__",
    "f_globals",
    "f_locals",
)


def _reject_dynamic_namespace_access(tree: ast.Module, symbols: Sequence[str]) -> None:
    """Reject mechanisms that can hide a contract-symbol read or write.

    A declaration-only symbol policy is not fail-closed if source can spell
    the same operation through ``globals()``, ``exec()``, frame namespaces,
    or module dictionaries.  These mechanisms have no legitimate role in a
    declarative contract module, so encountering one makes extraction fail.
    """
    protected = frozenset(symbols)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots = {alias.name.partition(".")[0] for alias in node.names}
            if not imported_roots <= _ALLOWED_CONTRACT_IMPORT_ROOTS:
                raise ContractSourceError(
                        "dynamic, reflective, or reassignment behavior from an unapproved import "
                    "prevents safe contract extraction."
                )
        if isinstance(node, ast.ImportFrom):
            imported_names = {alias.name for alias in node.names}
            imported_root = (node.module or "").partition(".")[0]
            if (
                imported_root not in _ALLOWED_CONTRACT_IMPORT_ROOTS
                or (node.module == "sys" and "modules" in imported_names)
                or (
                    node.module == "operator"
                    and imported_names & {"attrgetter", "methodcaller"}
                )
            ):
                raise ContractSourceError(
                        f"dynamic namespace helper or reassignment imported from {node.module!r} "
                    "prevents safe contract extraction."
                )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in protected or any(
                marker in node.value for marker in _REFLECTIVE_STRING_MARKERS
            ):
                raise ContractSourceError(
                    f"reflective string reference {node.value!r} prevents safe contract extraction."
                )
        if isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMESPACE_NAMES:
            raise ContractSourceError(
                f"dynamic namespace access through {node.id!r} prevents safe contract extraction."
            )
        if isinstance(node, ast.Attribute) and (
            node.attr in _DYNAMIC_NAMESPACE_ATTRIBUTES
            or node.attr in protected
            or (
                node.attr == "modules"
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
            )
        ):
            raise ContractSourceError(
                f"reflective attribute access through {node.attr!r} prevents safe contract extraction."
            )


def _reject_symbol_rebindings(
    tree: ast.Module,
    symbol: str,
    *,
    definition_node: ast.AST,
    definition_target: ast.Name | None = None,
    reject_loads: bool,
) -> None:
    """Reject every binding other than the selected literal/class definition.

    For literal constants, loads are rejected too: letting the object escape
    would make it impossible to prove that later runtime state still matches
    the AST literal. Enum loads are needed in the table and annotations, but
    every store/delete/rebinding of the enum name still fails.
    """
    _reject_dynamic_namespace_access(
        tree,
        (REGENGINE_TABLE_SYMBOL, CONTRACT_VERSION_SYMBOL, REGENGINE_ENUM_SYMBOL),
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == symbol
            and node is not definition_node
        ):
            raise ContractSourceError(
                f"{symbol} is reassigned by a later function or class definition."
            )

        if isinstance(node, ast.Import):
            bound_names = {
                alias.asname or alias.name.partition(".")[0]
                for alias in node.names
            }
            if symbol in bound_names:
                raise ContractSourceError(f"{symbol} is reassigned by an import.")

        if isinstance(node, ast.ImportFrom):
            bound_names = {alias.asname or alias.name for alias in node.names}
            if symbol in bound_names or "*" in bound_names:
                raise ContractSourceError(
                    f"{symbol} may be reassigned by a from-import."
                )

        if isinstance(node, ast.arg) and node.arg == symbol:
            raise ContractSourceError(
                f"{symbol} is rebound as a function argument."
            )

        if isinstance(node, ast.ExceptHandler) and node.name == symbol:
            raise ContractSourceError(f"{symbol} is reassigned by an exception handler.")

        if isinstance(node, (ast.Global, ast.Nonlocal)) and symbol in node.names:
            raise ContractSourceError(f"{symbol} is declared for reassignment in another scope.")

        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == symbol:
            raise ContractSourceError(f"{symbol} is reassigned by a pattern match.")

        if isinstance(node, ast.MatchMapping) and node.rest == symbol:
            raise ContractSourceError(f"{symbol} is reassigned by a pattern match.")

        if isinstance(node, ast.Name) and node.id == symbol and node is not definition_target:
            if reject_loads or isinstance(node.ctx, (ast.Store, ast.Del)):
                raise ContractSourceError(
                    f"{symbol} is referenced outside its single literal definition; "
                    "reassignment or mutation cannot be ruled out."
                )


def _enum_members(
    tree: ast.Module,
    enum_name: str,
    *,
    table_node: ast.Dict,
) -> dict[str, str]:
    """Map ``EnumName.MEMBER`` -> its string value."""
    future_annotation_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" and alias.asname is None for alias in node.names)
    ]
    if len(future_annotation_imports) != 1:
        raise ContractSourceError(
            "Contract source must enable `from __future__ import annotations` "
            "so enum annotations cannot expose mutable runtime objects."
        )
    definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == enum_name
    ]
    if len(definitions) != 1:
        raise ContractSourceError(
            f"Expected exactly one module-level `class {enum_name}` definition; "
            f"found {len(definitions)}."
        )

    definition = definitions[0]
    enum_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "enum"
        and any(alias.name == "Enum" and alias.asname is None for alias in node.names)
    ]
    base_names = [base.id for base in definition.bases if isinstance(base, ast.Name)]
    if (
        len(enum_imports) != 1
        or definition.decorator_list
        or definition.keywords
        or len(base_names) != len(definition.bases)
        or base_names != ["str", "Enum"]
    ):
        raise ContractSourceError(
            f"{enum_name} must be a plain declarative `class {enum_name}(str, Enum)` "
            "without decorators or metaclass customization."
        )
    enum_import = enum_imports[0]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id in {"str", "Enum"}
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node is not definition
            and node.name in {"str", "Enum"}
        ):
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node is not enum_import:
            bound_names = {
                alias.asname
                or (alias.name if isinstance(node, ast.ImportFrom) else alias.name.partition(".")[0])
                for alias in node.names
            }
            if bound_names & {"str", "Enum"} or "*" in bound_names:
                raise ContractSourceError(
                    f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
                )
        if isinstance(node, ast.arg) and node.arg in {"str", "Enum"}:
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
        if isinstance(node, ast.ExceptHandler) and node.name in {"str", "Enum"}:
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
        if isinstance(node, (ast.Global, ast.Nonlocal)) and {"str", "Enum"} & set(node.names):
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name in {"str", "Enum"}:
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
        if isinstance(node, ast.MatchMapping) and node.rest in {"str", "Enum"}:
            raise ContractSourceError(
                f"{enum_name} must use plain, unshadowed `str` and `Enum` bases."
            )
    _reject_symbol_rebindings(
        tree,
        enum_name,
        definition_node=definition,
        reject_loads=False,
    )

    allowed_loads = {
        id(key.value)
        for key in table_node.keys
        if isinstance(key, ast.Attribute)
        and isinstance(key.value, ast.Name)
        and key.value.id == enum_name
    }
    for node in ast.walk(tree):
        annotation: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            annotation = node.annotation
        elif isinstance(node, ast.arg):
            annotation = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation = node.returns
        if annotation is None:
            continue
        annotation_loads = [
            candidate
            for candidate in ast.walk(annotation)
            if isinstance(candidate, ast.Name) and candidate.id == enum_name
        ]
        if annotation_loads and any(isinstance(candidate, ast.Call) for candidate in ast.walk(annotation)):
            raise ContractSourceError(
                f"{enum_name} is used in an executable annotation; mutation cannot be ruled out."
            )
        allowed_loads.update(id(candidate) for candidate in annotation_loads)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == enum_name
            and isinstance(node.ctx, ast.Load)
            and id(node) not in allowed_loads
        ):
            raise ContractSourceError(
                f"{enum_name} is referenced outside declarative annotations and table keys; "
                "reassignment or mutation cannot be ruled out."
            )
    members: dict[str, str] = {}
    for index, statement in enumerate(definition.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.Pass):
            continue
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            raise ContractSourceError(
                f"{enum_name} must have a declarative body containing only "
                "unique literal string members."
            )
        target = statement.targets[0]
        value = statement.value
        if (
            not isinstance(target, ast.Name)
            or target.id.startswith("_")
            or not isinstance(value, ast.Constant)
            or not isinstance(value.value, str)
            or not value.value
        ):
            raise ContractSourceError(
                f"{enum_name} must have a declarative body containing only "
                "unique literal string members."
            )
        if target.id in members:
            raise ContractSourceError(
                f"{enum_name} has duplicate member {target.id!r}; its body must be declarative."
            )
        members[target.id] = value.value
    if not members:
        raise ContractSourceError(f"{enum_name} defines no string members.")
    return members


def _target_binds_symbol(target: ast.expr, symbol: str) -> bool:
    return any(
        isinstance(node, ast.Name)
        and node.id == symbol
        and isinstance(node.ctx, ast.Store)
        for node in ast.walk(target)
    )


def _single_simple_assignment(
    tree: ast.Module,
    symbol: str,
) -> tuple[ast.Assign | ast.AnnAssign, ast.Name]:
    definitions: list[tuple[ast.Assign | ast.AnnAssign, ast.Name]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if not any(_target_binds_symbol(target, symbol) for target in node.targets):
                continue
            if (
                len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)
                or node.targets[0].id != symbol
            ):
                raise ContractSourceError(
                    f"{symbol} must be the only assignment target in its definition."
                )
            definitions.append((node, node.targets[0]))
        elif isinstance(node, ast.AnnAssign):
            if not _target_binds_symbol(node.target, symbol):
                continue
            if not isinstance(node.target, ast.Name) or node.target.id != symbol:
                raise ContractSourceError(
                    f"{symbol} must be the only assignment target in its definition."
                )
            definitions.append((node, node.target))

    if len(definitions) != 1:
        raise ContractSourceError(
            f"Expected exactly one module-level `{symbol}` assignment; found {len(definitions)}."
        )
    return definitions[0]


def _table_node(tree: ast.Module, symbol: str) -> ast.Dict:
    definition, target = _single_simple_assignment(tree, symbol)
    if not isinstance(definition.value, ast.Dict):
        raise ContractSourceError(f"{symbol} is not a dict literal in RegEngine's source.")
    _reject_symbol_rebindings(
        tree,
        symbol,
        definition_node=definition,
        definition_target=target,
        reject_loads=True,
    )
    return definition.value


def extract_contract_version(source: str, symbol: str = CONTRACT_VERSION_SYMBOL) -> str:
    """Read one module-level literal contract-version assignment without imports."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ContractSourceError(f"Contract source did not parse: {exc}") from exc

    definition, target = _single_simple_assignment(tree, symbol)
    value = definition.value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not value.value:
        raise ContractSourceError(f"{symbol} must be a non-empty literal string.")
    _reject_symbol_rebindings(
        tree,
        symbol,
        definition_node=definition,
        definition_target=target,
        reject_loads=True,
    )
    return value.value


def contract_version_difference(inflow_source: str, regengine_source: str) -> str | None:
    """Describe a contract-version mismatch using AST-only source reads."""
    inflow_version = extract_contract_version(inflow_source)
    regengine_version = extract_contract_version(regengine_source)
    if inflow_version == regengine_version:
        return None
    return (
        f"Inflow Lab {CONTRACT_VERSION_SYMBOL} {inflow_version!r} != "
        f"RegEngine {CONTRACT_VERSION_SYMBOL} {regengine_version!r}"
    )


def extract_required_kdes(source: str) -> dict[str, tuple[str, ...]]:
    """Parse RegEngine's ``webhook_models.py`` text into ``{cte: (kde, ...)}``."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - upstream would be broken
        raise ContractSourceError(f"RegEngine's source did not parse: {exc}") from exc

    table_node = _table_node(tree, REGENGINE_TABLE_SYMBOL)
    members = _enum_members(
        tree,
        REGENGINE_ENUM_SYMBOL,
        table_node=table_node,
    )

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


def _decode_regengine_source(source: bytes, path: Path) -> str:
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractSourceError(f"Could not decode {path} as UTF-8: {exc}") from exc


def extract_from_checkout(regengine_root: Path) -> dict[str, tuple[str, ...]]:
    path = regengine_source_path(regengine_root)
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        raise ContractSourceError(f"Could not read {path}: {exc}") from exc
    return extract_required_kdes(_decode_regengine_source(source_bytes, path))


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
    source_sha256: str,
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
            "source_sha256": source_sha256,
            "extracted_by": "scripts/regengine_kde_contract.py",
            "extracted_at": extracted_at or datetime.now(UTC).date().isoformat(),
            "table_sha256": table_sha256(ordered),
            "regenerate": (
                "python scripts/regengine_kde_contract.py snapshot "
                "--regengine-root <regengine checkout> --source-commit <sha>"
            ),
            "enforced_by": (
                "tests/test_contract_provenance.py (checksum + shape, offline) and the "
                "trusted Cross-repo pin drift job in "
                ".github/workflows/contract-pin-drift.yml, which fetches RegEngine's "
                "source at a resolved immutable commit, verifies its exact source bytes, "
                "re-extracts this table and its contract version without executing upstream "
                "code, and fails on any difference."
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
    source_path = regengine_source_path(Path(args.regengine_root))
    source_bytes = source_path.read_bytes()
    table = extract_required_kdes(_decode_regengine_source(source_bytes, source_path))
    document = build_snapshot_document(
        table,
        source_commit=args.source_commit,
        source_sha256=source_bytes_sha256(source_bytes),
    )
    _write_json(document, args.output or str(DEFAULT_SNAPSHOT_PATH))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    snapshot = load_snapshot(args.snapshot)
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

    source_path = regengine_source_path(Path(args.regengine_root))
    source_bytes = source_path.read_bytes()
    fetched_source_sha256 = source_bytes_sha256(source_bytes)
    if snapshot.recorded_source_sha256 != fetched_source_sha256:
        print(
            "::error::RegEngine source provenance mismatch: snapshot source_sha256 "
            f"({snapshot.recorded_source_sha256 or 'missing'}) does not match the exact fetched "
            f"{REGENGINE_SOURCE_PATH} bytes ({fetched_source_sha256}). Refusing to parse source "
            "that is not bound to the reviewed snapshot; regenerate the snapshot from this exact "
            "immutable RegEngine source.",
            file=sys.stderr,
        )
        return EXIT_MISMATCH

    upstream_source = _decode_regengine_source(source_bytes, source_path)
    upstream = extract_required_kdes(upstream_source)
    inflow_source = Path(args.inflow_contract).read_text(encoding="utf-8")

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

    version_problem = contract_version_difference(inflow_source, upstream_source)
    if version_problem:
        print(f"::error::RegEngine ingest contract version drift -- {version_problem}", file=sys.stderr)
        print(
            f"::error::Bump {CONTRACT_VERSION_SYMBOL} on both sides only when the wire contract changes.",
            file=sys.stderr,
        )
        return EXIT_MISMATCH

    print(
        f"RegEngine required-KDE table matches the vendored snapshot "
        f"({len(upstream)} CTE types, table sha256 {recomputed}, "
        f"source sha256 {fetched_source_sha256}); "
        f"{CONTRACT_VERSION_SYMBOL} matches ({extract_contract_version(inflow_source)!r})."
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
    verify.add_argument(
        "--inflow-contract",
        default=str(REPO_ROOT / "app" / "contract.py"),
        help="Inflow Lab source file containing INFLOW_CONTRACT_VERSION.",
    )
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

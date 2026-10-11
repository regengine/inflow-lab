"""Provenance of the pinned RegEngine contract, and the pin-drift check (#104).

Before #104, ``tests/test_regengine_contract_pin.py`` held a hand-typed
copy of RegEngine's required-KDE table under a docstring claiming it was
"copied verbatim from RegEngine". Both sides of that assertion lived in
this repo, so it could only ever prove this repo agreed with itself. The
claim was unfalsifiable by construction.

The vendored table is now generated from RegEngine's real source by
``scripts/regengine_kde_contract.py`` and carries its provenance as data:
which repository, file, symbol and commit it was read from, a sha256 over
the exact source bytes, and a sha256 over the table itself. Two mechanisms
make that data load-bearing rather
than decorative:

* offline, in this file: the recorded table sha256 must match the file's
  own contents, so hand-editing the JSON instead of regenerating it fails;
* cross-repo, in the trusted ``Cross-repo pin drift`` job of
  ``.github/workflows/contract-pin-drift.yml``: RegEngine's source is fetched
  at a resolved immutable commit, its exact bytes must match the recorded
  source sha256 before parsing, and any scheduled/repository-dispatch source,
  table, or contract-version difference fails the gate.

The second half of this file covers ``scripts/contract_pin_drift.py``,
the check that fails when either repo's pin to the other has fallen too
far behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.cte_rules import REQUIRED_KDES
from app.schemas.domain import CTEType
from scripts import regengine_kde_contract as contract_tool
from scripts.contract_pin_drift import (
    CONTRACT_SURFACE_PRESETS,
    DriftInputError,
    evaluate_drift,
    extract_pinned_ref,
    matching_surface_files,
    render_summary,
)
from scripts.contract_pin_drift import (
    main as drift_main,
)
from scripts.regengine_kde_contract import (
    DEFAULT_SNAPSHOT_PATH,
    EXIT_MISMATCH,
    REGENGINE_ENUM_SYMBOL,
    REGENGINE_REPOSITORY,
    REGENGINE_SOURCE_PATH,
    REGENGINE_TABLE_SYMBOL,
    ContractSourceError,
    build_snapshot_document,
    contract_version_difference,
    extract_contract_version,
    extract_required_kdes,
    load_snapshot,
    source_bytes_sha256,
    table_sha256,
)

# Environment variable for the optional local "against the real upstream
# source" test. The trusted hosted gate invokes the same extractor directly
# against its immutable source fetch rather than installing either project.
#   REGENGINE_CHECKOUT=/path/to/regengine pytest tests/test_contract_provenance.py
REGENGINE_CHECKOUT_ENV = "REGENGINE_CHECKOUT"


UPSTREAM_FIXTURE = '''
from __future__ import annotations

from enum import Enum


INFLOW_CONTRACT_VERSION = "2"


class WebhookCTEType(str, Enum):
    GROWING = "growing"  # legacy, not in the table
    HARVESTING = "harvesting"
    SHIPPING = "shipping"


REQUIRED_KDES_BY_CTE: Dict[WebhookCTEType, List[str]] = {
    WebhookCTEType.HARVESTING: [
        "traceability_lot_code",
        "harvest_date",  # a trailing comment
    ],
    WebhookCTEType.SHIPPING: [
        "traceability_lot_code",
        "ship_date",
    ],
}
'''


# ---------------------------------------------------------------------------
# Reading RegEngine's table out of RegEngine's source
# ---------------------------------------------------------------------------


def test_extractor_resolves_enum_keys_to_wire_values() -> None:
    table = extract_required_kdes(UPSTREAM_FIXTURE)

    assert table == {
        "harvesting": ("traceability_lot_code", "harvest_date"),
        "shipping": ("traceability_lot_code", "ship_date"),
    }
    # A CTE type upstream declares but does not put in the table must not
    # appear: the table is the contract, the enum is not.
    assert "growing" not in table


def test_extractor_requires_inert_future_annotations() -> None:
    source = UPSTREAM_FIXTURE.replace("from __future__ import annotations\n\n", "")

    with pytest.raises(ContractSourceError, match="future|annotations"):
        extract_required_kdes(source)


def test_extractor_refuses_a_source_it_cannot_resolve() -> None:
    """Every failure here means "upstream moved the contract", which has to
    be loud. A partial table would look like a contract that shrank."""
    without_enum = UPSTREAM_FIXTURE.replace("class WebhookCTEType(str, Enum):", "class Other(str, Enum):")
    with pytest.raises(ContractSourceError, match=REGENGINE_ENUM_SYMBOL):
        extract_required_kdes(without_enum)

    without_table = UPSTREAM_FIXTURE.replace("REQUIRED_KDES_BY_CTE:", "SOMETHING_ELSE:")
    with pytest.raises(ContractSourceError, match=REGENGINE_TABLE_SYMBOL):
        extract_required_kdes(without_table)

    computed_keys = UPSTREAM_FIXTURE.replace("WebhookCTEType.HARVESTING:", '"harvesting":')
    with pytest.raises(ContractSourceError):
        extract_required_kdes(computed_keys)

    non_literal_values = UPSTREAM_FIXTURE.replace('"harvest_date",  # a trailing comment', "SOME_CONSTANT,")
    with pytest.raises(ContractSourceError):
        extract_required_kdes(non_literal_values)


def test_extractor_handles_a_plain_unannotated_assignment() -> None:
    unannotated = UPSTREAM_FIXTURE.replace(
        "REQUIRED_KDES_BY_CTE: Dict[WebhookCTEType, List[str]] =", "REQUIRED_KDES_BY_CTE ="
    )
    assert extract_required_kdes(unannotated) == extract_required_kdes(UPSTREAM_FIXTURE)


def test_extractor_rejects_multiple_table_assignments() -> None:
    duplicate = UPSTREAM_FIXTURE + "\nREQUIRED_KDES_BY_CTE = {}\n"

    with pytest.raises(ContractSourceError, match="exactly one"):
        extract_required_kdes(duplicate)


def test_extractor_rejects_a_chained_table_definition_that_creates_an_alias() -> None:
    chained = UPSTREAM_FIXTURE.replace(
        "REQUIRED_KDES_BY_CTE: Dict[WebhookCTEType, List[str]] =",
        "alias = REQUIRED_KDES_BY_CTE =",
    )

    with pytest.raises(ContractSourceError, match="only assignment target"):
        extract_required_kdes(chained)


@pytest.mark.parametrize(
    "rebind",
    (
        "def REQUIRED_KDES_BY_CTE():\n    return {}",
        "class REQUIRED_KDES_BY_CTE:\n    pass",
        "from replacement import table as REQUIRED_KDES_BY_CTE",
    ),
)
def test_extractor_rejects_non_assignment_rebinding_of_the_table_name(rebind: str) -> None:
    with pytest.raises(ContractSourceError, match="reassign"):
        extract_required_kdes(UPSTREAM_FIXTURE + f"\n{rebind}\n")


@pytest.mark.parametrize(
    "mutation",
    (
        "REQUIRED_KDES_BY_CTE[WebhookCTEType.HARVESTING] = ['replacement']",
        "REQUIRED_KDES_BY_CTE[WebhookCTEType.HARVESTING].append('late_kde')",
        "REQUIRED_KDES_BY_CTE.get(WebhookCTEType.HARVESTING).append('late_kde')",
        "REQUIRED_KDES_BY_CTE.update({WebhookCTEType.HARVESTING: ['replacement']})",
        "REQUIRED_KDES_BY_CTE.__setitem__(WebhookCTEType.HARVESTING, ['replacement'])",
        "REQUIRED_KDES_BY_CTE |= {WebhookCTEType.HARVESTING: ['replacement']}",
        "alias = REQUIRED_KDES_BY_CTE\nalias.clear()",
        "def mutate(table):\n    table.clear()\nmutate(REQUIRED_KDES_BY_CTE)",
        "def mutate(*tables):\n    tables[0].clear()\nmutate(*(REQUIRED_KDES_BY_CTE,))",
        (
            "for value in REQUIRED_KDES_BY_CTE.values():\n"
            "    value.append('late_kde')"
        ),
    ),
)
def test_extractor_rejects_table_mutation_after_the_literal_assignment(mutation: str) -> None:
    with pytest.raises(ContractSourceError, match="mutat|reassign"):
        extract_required_kdes(UPSTREAM_FIXTURE + f"\n{mutation}\n")


@pytest.mark.parametrize(
    "mutation",
    (
        (
            'globals()["REQUIRED_KDES_BY_CTE"]'
            "[WebhookCTEType.HARVESTING].append('late_kde')"
        ),
        'globals()["REQUIRED_KDES_BY_CTE"] = {}',
        'exec("REQUIRED_KDES_BY_CTE = {}")',
        (
            "from builtins import globals as namespace\n"
            'namespace()["REQUIRED_KDES_BY_CTE"].clear()'
        ),
        (
            "from sys import modules as registry\n"
            "object.__getattribute__(registry[__name__], "
            '"REQUIRED_KDES_BY_CTE").clear()'
        ),
        (
            "from builtins import __dict__ as builtins_namespace\n"
            'builtins_namespace["globals"]()["REQUIRED_KDES_BY_CTE"]'
            "[WebhookCTEType.HARVESTING].append('late_kde')"
        ),
        (
            "import inspect\n"
            "def anchor():\n"
            "    pass\n"
            "inspect.getattr_static(inspect.getmodule(anchor), "
            '"REQUIRED_KDES_BY_CTE")[WebhookCTEType.HARVESTING]'
            ".append('late_kde')"
        ),
        (
            "import string\n"
            "def anchor():\n"
            "    pass\n"
            "table = string.Formatter().get_field("
            '"0.__globals__[REQUIRED_KDES_BY_CTE]", (anchor,), {})[0]\n'
            "next(iter(table.values())).append('late_kde')"
        ),
    ),
)
def test_extractor_rejects_reflective_table_mutation(mutation: str) -> None:
    with pytest.raises(ContractSourceError, match="dynamic|reflect|mutat|reassign"):
        extract_required_kdes(UPSTREAM_FIXTURE + f"\n{mutation}\n")


def test_extractor_rejects_duplicate_enum_definitions() -> None:
    duplicate = UPSTREAM_FIXTURE.replace(
        "REQUIRED_KDES_BY_CTE: Dict[WebhookCTEType, List[str]] =",
        '''
class WebhookCTEType(str, Enum):
    GROWING = "runtime-growing"
    HARVESTING = "runtime-harvesting"
    SHIPPING = "runtime-shipping"


REQUIRED_KDES_BY_CTE: Dict[WebhookCTEType, List[str]] =''',
    )

    with pytest.raises(ContractSourceError, match="exactly one"):
        extract_required_kdes(duplicate)


@pytest.mark.parametrize(
    "replacement",
    (
        "@replace_enum\nclass WebhookCTEType(str, Enum):",
        "class WebhookCTEType(str, Enum, metaclass=RuntimeMeta):",
        "class WebhookCTEType(str, RuntimeEnum):",
        "Enum = RuntimeEnum\nclass WebhookCTEType(str, Enum):",
        "str = RuntimeString\nclass WebhookCTEType(str, Enum):",
    ),
)
def test_extractor_requires_a_plain_string_enum_definition(replacement: str) -> None:
    source = UPSTREAM_FIXTURE.replace(
        "class WebhookCTEType(str, Enum):",
        replacement,
    )

    with pytest.raises(ContractSourceError, match="plain|declarative"):
        extract_required_kdes(source)


def test_extractor_rejects_runtime_behavior_in_the_enum_body() -> None:
    source = UPSTREAM_FIXTURE.replace(
        '    GROWING = "growing"  # legacy, not in the table',
        '''    def __new__(cls, value):
        member = str.__new__(cls, value)
        member._value_ = f"runtime-{value}"
        return member

    GROWING = "growing"  # legacy, not in the table''',
    )

    with pytest.raises(ContractSourceError, match="declarative"):
        extract_required_kdes(source)


def test_extractor_rejects_duplicate_enum_members() -> None:
    source = UPSTREAM_FIXTURE.replace(
        '    SHIPPING = "shipping"',
        '    HARVESTING = "runtime-harvesting"',
    )

    with pytest.raises(ContractSourceError, match="duplicate|declarative"):
        extract_required_kdes(source)


def test_extractor_rejects_mutating_an_enum_member_after_definition() -> None:
    mutation = 'WebhookCTEType.HARVESTING._value_ = "runtime-harvesting"'

    with pytest.raises(ContractSourceError, match="mutat|reassign|outside"):
        extract_required_kdes(UPSTREAM_FIXTURE + f"\n{mutation}\n")


def test_extractor_rejects_accessing_an_enum_through_runtime_annotations() -> None:
    mutation = (
        "alias: WebhookCTEType\n"
        '__annotations__["alias"].HARVESTING._value_ = "runtime-harvesting"'
    )

    with pytest.raises(ContractSourceError, match="dynamic|reflect|mutat|reassign"):
        extract_required_kdes(UPSTREAM_FIXTURE + f"\n{mutation}\n")


def test_extractor_rejects_a_pattern_capture_that_shadows_an_enum_base() -> None:
    source = UPSTREAM_FIXTURE.replace(
        "class WebhookCTEType(str, Enum):",
        '''class RuntimeString(str):
    def __new__(cls, value):
        return super().__new__(cls, f"runtime-{value}")


match RuntimeString:
    case str:
        pass


class WebhookCTEType(str, Enum):''',
    )

    with pytest.raises(ContractSourceError, match="plain|unshadowed"):
        extract_required_kdes(source)


def test_contract_version_extractor_requires_one_literal_string_assignment() -> None:
    assert extract_contract_version(UPSTREAM_FIXTURE) == "2"

    with pytest.raises(ContractSourceError, match="INFLOW_CONTRACT_VERSION"):
        extract_contract_version(UPSTREAM_FIXTURE.replace('INFLOW_CONTRACT_VERSION = "2"', ""))

    duplicate = UPSTREAM_FIXTURE + '\nINFLOW_CONTRACT_VERSION = "3"\n'
    with pytest.raises(ContractSourceError, match="exactly one"):
        extract_contract_version(duplicate)

    computed = UPSTREAM_FIXTURE.replace('INFLOW_CONTRACT_VERSION = "2"', "INFLOW_CONTRACT_VERSION = str(2)")
    with pytest.raises(ContractSourceError, match="literal string"):
        extract_contract_version(computed)


def test_contract_version_extractor_rejects_a_chained_definition() -> None:
    chained = UPSTREAM_FIXTURE.replace(
        'INFLOW_CONTRACT_VERSION = "2"',
        'alias = INFLOW_CONTRACT_VERSION = "2"',
    )

    with pytest.raises(ContractSourceError, match="only assignment target"):
        extract_contract_version(chained)


@pytest.mark.parametrize(
    "rebind",
    (
        'INFLOW_CONTRACT_VERSION += ".changed"',
        "from replacement import version as INFLOW_CONTRACT_VERSION",
        'globals()["INFLOW_CONTRACT_VERSION"] = "3"',
        'exec("INFLOW_CONTRACT_VERSION = \\"3\\"")',
        (
            "from builtins import __dict__ as builtins_namespace\n"
            'builtins_namespace["globals"]()["INFLOW_CONTRACT_VERSION"] = "3"'
        ),
        (
            "import string\n"
            "def anchor():\n"
            "    pass\n"
            "namespace = string.Formatter().get_field("
            '"0.__globals__", (anchor,), {})[0]\n'
            'namespace["INFLOW_CONTRACT_" + "VERSION"] = "3"'
        ),
    ),
)
def test_contract_version_extractor_rejects_later_rebinding(rebind: str) -> None:
    with pytest.raises(ContractSourceError, match="dynamic|reflect|mutat|reassign"):
        extract_contract_version(UPSTREAM_FIXTURE + f"\n{rebind}\n")


def test_contract_version_comparison_reports_skew_without_executing_either_source() -> None:
    inflow = 'INFLOW_CONTRACT_VERSION = "2"\n'
    regengine = 'INFLOW_CONTRACT_VERSION = "3"\n'

    assert contract_version_difference(inflow, inflow) is None
    assert contract_version_difference(inflow, regengine) == (
        "Inflow Lab INFLOW_CONTRACT_VERSION '2' != RegEngine INFLOW_CONTRACT_VERSION '3'"
    )


# ---------------------------------------------------------------------------
# The vendored snapshot and its provenance
# ---------------------------------------------------------------------------


def test_snapshot_checksum_matches_its_own_contents() -> None:
    """The check that makes provenance machine-checkable.

    Editing the vendored table by hand -- the thing the old "copied
    verbatim" comment invited -- leaves the recorded digest behind and
    fails here, forcing a regeneration from RegEngine's source instead.
    """
    snapshot = load_snapshot()

    assert snapshot.recorded_table_sha256, "snapshot has no recorded table_sha256"
    assert snapshot.recorded_table_sha256 == table_sha256(snapshot.table)


def test_snapshot_binds_the_exact_upstream_source_bytes() -> None:
    snapshot = load_snapshot()

    assert len(snapshot.recorded_source_sha256) == 64, "snapshot has no valid source_sha256"
    assert set(snapshot.recorded_source_sha256) <= set("0123456789abcdef")


def test_snapshot_records_where_upstream_actually_keeps_the_table() -> None:
    provenance = load_snapshot().provenance

    assert provenance["source_repository"] == REGENGINE_REPOSITORY
    assert provenance["source_path"] == REGENGINE_SOURCE_PATH
    assert provenance["source_symbol"] == REGENGINE_TABLE_SYMBOL
    assert provenance["source_enum"] == REGENGINE_ENUM_SYMBOL
    assert provenance["extracted_by"] == "scripts/regengine_kde_contract.py"
    assert "contract-pin-drift.yml" in provenance["enforced_by"]
    assert "ci.yml" not in provenance["enforced_by"]
    # A real commit, not a placeholder: without it there is no way to look
    # up what upstream looked like when this was taken.
    assert len(provenance["source_commit"]) == 40
    assert set(provenance["source_commit"]) <= set("0123456789abcdef")


def test_running_table_equals_the_snapshot_taken_from_regengine() -> None:
    """app.cte_rules.REQUIRED_KDES is what the simulator validates with;
    the snapshot is what RegEngine validates with. Same assertion the pin
    test makes, stated against the generated artifact."""
    running = {cte_type.value: tuple(kdes) for cte_type, kdes in REQUIRED_KDES.items()}

    assert running == load_snapshot().table


def test_snapshot_covers_exactly_this_repos_cte_types() -> None:
    assert set(load_snapshot().table) == {cte_type.value for cte_type in CTEType}


def test_checksum_tracks_kde_order_but_not_cte_key_order() -> None:
    """KDE order is part of what the pin test asserts (it compares tuples),
    so the digest has to move when it changes; CTE key order is just JSON
    formatting, so it must not."""
    table = {"harvesting": ["a", "b"], "shipping": ["c"]}
    reordered_keys = {"shipping": ["c"], "harvesting": ["a", "b"]}
    reordered_kdes = {"harvesting": ["b", "a"], "shipping": ["c"]}

    assert table_sha256(table) == table_sha256(reordered_keys)
    assert table_sha256(table) != table_sha256(reordered_kdes)


def test_source_checksum_tracks_exact_bytes_without_newline_normalization() -> None:
    lf = b"first line\nsecond line\n"
    crlf = b"first line\r\nsecond line\r\n"

    assert source_bytes_sha256(lf) != source_bytes_sha256(crlf)


def test_snapshot_document_round_trips_through_the_loader(tmp_path: Path) -> None:
    source_sha256 = source_bytes_sha256(UPSTREAM_FIXTURE.encode("utf-8"))
    document = build_snapshot_document(
        extract_required_kdes(UPSTREAM_FIXTURE),
        source_commit="a" * 40,
        source_sha256=source_sha256,
        extracted_at="2026-01-01",
    )
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_snapshot(path)

    assert loaded.table == {
        "harvesting": ("traceability_lot_code", "harvest_date"),
        "shipping": ("traceability_lot_code", "ship_date"),
    }
    assert loaded.recorded_table_sha256 == table_sha256(loaded.table)
    assert loaded.recorded_source_sha256 == source_sha256


def _write_verify_fixture(
    tmp_path: Path,
    *,
    upstream_source: bytes,
    snapshot_document: dict[str, Any],
) -> tuple[Path, Path, Path]:
    regengine_root = tmp_path / "regengine"
    source_path = regengine_root / REGENGINE_SOURCE_PATH
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(upstream_source)

    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot_document), encoding="utf-8")
    inflow_contract = tmp_path / "contract.py"
    inflow_contract.write_text('INFLOW_CONTRACT_VERSION = "2"\n', encoding="utf-8")
    return regengine_root, snapshot_path, inflow_contract


def _run_verify(regengine_root: Path, snapshot_path: Path, inflow_contract: Path) -> int:
    return contract_tool.main(
        [
            "verify",
            "--regengine-root",
            str(regengine_root),
            "--snapshot",
            str(snapshot_path),
            "--inflow-contract",
            str(inflow_contract),
        ]
    )


def test_verify_rejects_a_missing_source_digest_before_ast_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source_bytes = UPSTREAM_FIXTURE.encode("utf-8")
    document = build_snapshot_document(
        extract_required_kdes(UPSTREAM_FIXTURE),
        source_commit="a" * 40,
        source_sha256=source_bytes_sha256(source_bytes),
        extracted_at="2026-01-01",
    )
    del document["provenance"]["source_sha256"]
    regengine_root, snapshot_path, inflow_contract = _write_verify_fixture(
        tmp_path,
        upstream_source=source_bytes,
        snapshot_document=document,
    )
    monkeypatch.setattr(
        contract_tool,
        "extract_required_kdes",
        lambda _source: pytest.fail("AST extraction ran before source provenance validation"),
    )

    assert _run_verify(regengine_root, snapshot_path, inflow_contract) == EXIT_MISMATCH
    assert "source_sha256 (missing)" in capsys.readouterr().err


def test_verify_rejects_adversarial_source_byte_drift_before_ast_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trusted_bytes = UPSTREAM_FIXTURE.encode("utf-8")
    document = build_snapshot_document(
        extract_required_kdes(UPSTREAM_FIXTURE),
        source_commit="a" * 40,
        source_sha256=source_bytes_sha256(trusted_bytes),
        extracted_at="2026-01-01",
    )
    adversarial_source = (
        UPSTREAM_FIXTURE
        + '''\nimport typing\ndef attack(x: "globals()['REQUIRED_KDES_BY_CTE'][WebhookCTEType.HARVESTING].append('late')"):\n'''
        + "    pass\ntyping.get_type_hints(attack)\n"
    ).encode("utf-8")
    regengine_root, snapshot_path, inflow_contract = _write_verify_fixture(
        tmp_path,
        upstream_source=adversarial_source,
        snapshot_document=document,
    )
    monkeypatch.setattr(
        contract_tool,
        "extract_required_kdes",
        lambda _source: pytest.fail("AST extraction ran before source provenance validation"),
    )

    assert _run_verify(regengine_root, snapshot_path, inflow_contract) == EXIT_MISMATCH
    error = capsys.readouterr().err
    assert "source_sha256" in error
    assert source_bytes_sha256(trusted_bytes) in error
    assert source_bytes_sha256(adversarial_source) in error


def test_verify_accepts_source_bytes_bound_to_the_snapshot(tmp_path: Path) -> None:
    source_bytes = UPSTREAM_FIXTURE.encode("utf-8")
    document = build_snapshot_document(
        extract_required_kdes(UPSTREAM_FIXTURE),
        source_commit="a" * 40,
        source_sha256=source_bytes_sha256(source_bytes),
        extracted_at="2026-01-01",
    )
    regengine_root, snapshot_path, inflow_contract = _write_verify_fixture(
        tmp_path,
        upstream_source=source_bytes,
        snapshot_document=document,
    )

    assert _run_verify(regengine_root, snapshot_path, inflow_contract) == 0


def test_loader_rejects_a_document_that_is_not_a_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "not-a-snapshot.json"
    path.write_text(json.dumps({"harvesting": ["traceability_lot_code"]}), encoding="utf-8")

    with pytest.raises(ContractSourceError):
        load_snapshot(path)


@pytest.mark.skipif(
    not os.getenv(REGENGINE_CHECKOUT_ENV),
    reason=f"set {REGENGINE_CHECKOUT_ENV} to a RegEngine checkout to check the snapshot against upstream",
)
def test_snapshot_matches_a_real_regengine_checkout() -> None:
    """The same table assertion the trusted gate enforces, available to
    anyone who has both repositories checked out locally."""
    from scripts.regengine_kde_contract import extract_from_checkout

    upstream = extract_from_checkout(Path(os.environ[REGENGINE_CHECKOUT_ENV]))

    assert load_snapshot().table == upstream


# ---------------------------------------------------------------------------
# Pin drift
# ---------------------------------------------------------------------------


def _comparison(*, ahead_by: int, files: list[str], total_commits: int | None = None) -> dict[str, Any]:
    commits = [{"sha": f"{index:040x}"} for index in range(ahead_by)]
    return {
        "status": "ahead" if ahead_by else "identical",
        "ahead_by": ahead_by,
        "behind_by": 0,
        "total_commits": ahead_by if total_commits is None else total_commits,
        "commits": commits,
        "files": [{"filename": name} for name in files],
    }


def test_a_pin_within_threshold_with_no_surface_change_passes() -> None:
    report = evaluate_drift(
        _comparison(ahead_by=4, files=["README.md", "app/static/app.js"]),
        max_commits=10,
        surface_patterns=CONTRACT_SURFACE_PRESETS["inflow-lab"],
    )

    assert not report.breached
    assert report.reasons == ()


def test_commit_count_past_the_threshold_breaches() -> None:
    report = evaluate_drift(
        _comparison(ahead_by=11, files=["docs/whatever.md"]),
        max_commits=10,
        surface_patterns=CONTRACT_SURFACE_PRESETS["inflow-lab"],
    )

    assert report.breached
    assert "11 commits behind" in report.reasons[0]


def test_one_contract_surface_commit_breaches_below_the_threshold() -> None:
    """The condition that made #104 real: the pin was only a handful of
    commits behind, but one of them (#85) rewrote the controller's
    verdict pairing, so the pinned commit no longer exercised the contract
    that shipped."""
    report = evaluate_drift(
        _comparison(ahead_by=2, files=["app/controller.py", "README.md"]),
        max_commits=10,
        surface_patterns=CONTRACT_SURFACE_PRESETS["inflow-lab"],
    )

    assert report.breached
    assert report.surface_files == ("app/controller.py",)
    assert any("app/controller.py" in reason for reason in report.reasons)


def test_a_truncated_comparison_is_treated_as_drifted() -> None:
    """GitHub caps compare responses. An empty file list past the cap means
    "unknown", and calling unknown "clean" is the silent pass this check
    exists to remove."""
    report = evaluate_drift(
        _comparison(ahead_by=400, files=[], total_commits=400),
        max_commits=1000,
        surface_patterns=CONTRACT_SURFACE_PRESETS["inflow-lab"],
    )

    assert report.truncated
    assert report.breached
    assert any("truncated" in reason for reason in report.reasons)


def test_truncation_is_not_reported_twice_when_a_surface_file_already_breached() -> None:
    report = evaluate_drift(
        _comparison(ahead_by=400, files=["app/contract.py"], total_commits=400),
        max_commits=1000,
        surface_patterns=CONTRACT_SURFACE_PRESETS["inflow-lab"],
    )

    assert report.truncated
    assert [reason for reason in report.reasons if "truncated" in reason] == []


def test_malformed_comparison_payloads_raise() -> None:
    with pytest.raises(DriftInputError):
        evaluate_drift({"files": []}, max_commits=10, surface_patterns=())
    with pytest.raises(DriftInputError):
        evaluate_drift({"ahead_by": "lots"}, max_commits=10, surface_patterns=())
    with pytest.raises(DriftInputError):
        evaluate_drift({"ahead_by": 1, "files": "nope"}, max_commits=10, surface_patterns=())


@pytest.mark.parametrize(
    "missing",
    ("status", "ahead_by", "behind_by", "total_commits", "commits", "files"),
)
def test_comparison_requires_the_complete_github_schema(missing: str) -> None:
    comparison = _comparison(ahead_by=1, files=[])
    del comparison[missing]

    with pytest.raises(DriftInputError, match=missing):
        evaluate_drift(comparison, max_commits=10, surface_patterns=())


@pytest.mark.parametrize("field", ("ahead_by", "behind_by", "total_commits"))
@pytest.mark.parametrize("value", (-1, True, "1", 1.5))
def test_comparison_rejects_negative_or_non_integer_counts(field: str, value: object) -> None:
    comparison = _comparison(ahead_by=1, files=[])
    comparison[field] = value

    with pytest.raises(DriftInputError, match=field):
        evaluate_drift(comparison, max_commits=10, surface_patterns=())


@pytest.mark.parametrize("status", ("behind", "diverged", "unknown"))
def test_comparison_rejects_non_ancestral_histories(status: str) -> None:
    comparison = _comparison(ahead_by=1, files=[])
    comparison["status"] = status
    comparison["behind_by"] = 4

    with pytest.raises(DriftInputError, match="status"):
        evaluate_drift(comparison, max_commits=10, surface_patterns=())


def test_comparison_rejects_inconsistent_or_malformed_history_entries() -> None:
    inconsistent = _comparison(ahead_by=1, files=[])
    inconsistent["behind_by"] = 1
    with pytest.raises(DriftInputError, match="behind_by"):
        evaluate_drift(inconsistent, max_commits=10, surface_patterns=())

    malformed_commit = _comparison(ahead_by=1, files=[])
    malformed_commit["commits"] = [{}]
    with pytest.raises(DriftInputError, match="commits"):
        evaluate_drift(malformed_commit, max_commits=10, surface_patterns=())

    malformed_file = _comparison(ahead_by=1, files=[])
    malformed_file["files"] = [{}]
    with pytest.raises(DriftInputError, match="files"):
        evaluate_drift(malformed_file, max_commits=10, surface_patterns=())


def test_file_truncation_uses_raw_records_at_the_299_300_boundary() -> None:
    below = _comparison(ahead_by=1, files=[f"docs/{index}.md" for index in range(299)])
    at_limit = _comparison(ahead_by=1, files=[f"docs/{index}.md" for index in range(300)])

    assert not evaluate_drift(below, max_commits=10, surface_patterns=()).truncated
    assert evaluate_drift(at_limit, max_commits=10, surface_patterns=()).truncated


def test_commit_truncation_uses_the_249_250_boundary() -> None:
    below = _comparison(ahead_by=249, files=[])
    at_limit = _comparison(ahead_by=250, files=[])

    assert not evaluate_drift(below, max_commits=1000, surface_patterns=()).truncated
    assert evaluate_drift(at_limit, max_commits=1000, surface_patterns=()).truncated


def test_a_renamed_contract_surface_file_matches_its_previous_name() -> None:
    comparison = _comparison(ahead_by=1, files=[])
    comparison["files"] = [
        {
            "filename": "archive/regengine_client.py",
            "previous_filename": "app/regengine_client.py",
            "status": "renamed",
        }
    ]

    report = evaluate_drift(
        comparison,
        max_commits=10,
        surface_patterns=CONTRACT_SURFACE_PRESETS["inflow-lab"],
    )

    assert report.breached
    assert "app/regengine_client.py" in report.surface_files


def test_surface_presets_cover_the_files_the_contract_job_gates_on() -> None:
    """These presets are the machine-readable half of the same statement
    the workflow's path filter makes; if they diverge the drift check stops
    watching what CI watches."""
    inflow = CONTRACT_SURFACE_PRESETS["inflow-lab"]
    assert matching_surface_files(
        [
            "app/regengine_client.py",
            "app/controller.py",
            "app/schemas/domain.py",
            "app/routers/ingestion.py",
            "app/routers/mock_regengine.py",
            "app/contract.py",
            "scripts/contract_pin_drift.py",
            "scripts/regengine_kde_contract.py",
            ".github/workflows/contract-pin-drift.yml",
        ],
        inflow,
    ) == (
        ".github/workflows/contract-pin-drift.yml",
        "app/contract.py",
        "app/controller.py",
        "app/regengine_client.py",
        "app/routers/ingestion.py",
        "app/routers/mock_regengine.py",
        "app/schemas/domain.py",
        "scripts/contract_pin_drift.py",
        "scripts/regengine_kde_contract.py",
    )
    assert matching_surface_files(["app/static/app.js", "README.md"], inflow) == ()

    regengine = CONTRACT_SURFACE_PRESETS["regengine"]
    assert matching_surface_files(
        [
            ".github/workflows/inflow-contract-ci.yml",
            "services/ingestion/app/webhook_models.py",
            "services/ingestion/app/webhook_router_v2/routes.py",
        ],
        regengine,
    ) == (
        ".github/workflows/inflow-contract-ci.yml",
        "services/ingestion/app/webhook_models.py",
        "services/ingestion/app/webhook_router_v2/routes.py",
    )


@pytest.mark.parametrize(
    "path",
    (
        "app/delivery.py",
        "app/engine.py",
        "app/routers/simulation.py",
        "app/main.py",
        "pyproject.toml",
        "uv.lock",
    ),
)
def test_inflow_contract_surface_covers_live_runtime_and_dependency_paths(path: str) -> None:
    assert matching_surface_files([path], CONTRACT_SURFACE_PRESETS["inflow-lab"]) == (path,)


def test_inflow_contract_surface_covers_every_server_python_module() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_paths = tuple(
        sorted(str(path.relative_to(repo_root)) for path in (repo_root / "app").rglob("*.py"))
    )

    assert matching_surface_files(runtime_paths, CONTRACT_SURFACE_PRESETS["inflow-lab"]) == runtime_paths


def test_extract_pinned_ref_reads_the_sha_off_an_env_line() -> None:
    workflow = "env:\n  INFLOW_LAB_REF: 4651e1a8e3562e44eadf65a67935276d754325a3  # pragma: allowlist secret\n"

    assert extract_pinned_ref(workflow) == "4651e1a8e3562e44eadf65a67935276d754325a3"


def test_extract_pinned_ref_rejects_short_or_duplicate_pins() -> None:
    with pytest.raises(DriftInputError, match="40-character"):
        extract_pinned_ref('  INFLOW_LAB_REF: "abc1234"\n')

    duplicate = (
        "env:\n"
        "  INFLOW_LAB_REF: 4651e1a8e3562e44eadf65a67935276d754325a3\n"
        "jobs:\n"
        "  demo:\n"
        "    env:\n"
        "      INFLOW_LAB_REF: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    with pytest.raises(DriftInputError, match="exactly one"):
        extract_pinned_ref(duplicate)


def test_extract_pinned_ref_raises_when_upstream_moved_the_pin() -> None:
    with pytest.raises(DriftInputError, match="INFLOW_LAB_REF"):
        extract_pinned_ref("env:\n  SOMETHING_ELSE: 4651e1a\n")


def _run_drift_cli(tmp_path: Path, comparison: dict[str, Any], *extra: str) -> int:
    path = tmp_path / "comparison.json"
    path.write_text(json.dumps(comparison), encoding="utf-8")
    return drift_main(
        [
            "check",
            "--comparison",
            str(path),
            "--label",
            "test pin",
            "--max-commits",
            "10",
            "--surface-preset",
            "inflow-lab",
            *extra,
        ]
    )


def test_cli_fails_on_breach_when_this_repo_can_fix_it(tmp_path: Path) -> None:
    assert _run_drift_cli(tmp_path, _comparison(ahead_by=99, files=[])) == 1


def test_cli_only_warns_on_breach_when_the_other_repo_owns_the_pin(tmp_path: Path) -> None:
    """A pull request here cannot advance RegEngine's INFLOW_LAB_REF, so on
    pull requests the drift is annotated rather than used to block a
    contributor; the scheduled run passes --on-breach fail."""
    assert _run_drift_cli(tmp_path, _comparison(ahead_by=99, files=[]), "--on-breach", "warn") == 0


def test_cli_passes_a_clean_comparison(tmp_path: Path) -> None:
    assert _run_drift_cli(tmp_path, _comparison(ahead_by=1, files=["README.md"])) == 0


def test_cli_reports_bad_input_distinctly_from_drift(tmp_path: Path) -> None:
    path = tmp_path / "comparison.json"
    path.write_text("not json", encoding="utf-8")

    assert drift_main(
        ["check", "--comparison", str(path), "--label", "test pin", "--max-commits", "10"]
    ) == 3


def test_cli_prints_the_ref_a_workflow_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The workflow reads RegEngine's INFLOW_LAB_REF through this command
    rather than a shell regex, so the parsing is covered by tests."""
    workflow = tmp_path / "inflow-contract-ci.yml"
    workflow.write_text(
        "env:\n  INFLOW_LAB_REF: 4651e1a8e3562e44eadf65a67935276d754325a3  # pragma: allowlist secret\n",
        encoding="utf-8",
    )

    assert drift_main(["pinned-ref", "--workflow", str(workflow)]) == 0
    assert capsys.readouterr().out.strip() == "4651e1a8e3562e44eadf65a67935276d754325a3"


def test_cli_fails_when_the_pinned_ref_is_gone(tmp_path: Path) -> None:
    workflow = tmp_path / "inflow-contract-ci.yml"
    workflow.write_text("env:\n  PYTHON_VERSION: '3.11'\n", encoding="utf-8")

    assert drift_main(["pinned-ref", "--workflow", str(workflow)]) == 3


def test_cli_writes_a_step_summary_when_github_asks_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    _run_drift_cli(tmp_path, _comparison(ahead_by=2, files=["app/controller.py"]))

    written = summary.read_text(encoding="utf-8")
    assert "Contract pin drift" in written
    assert "app/controller.py" in written


def test_cli_escapes_untrusted_text_in_actions_commands_and_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    malicious_name = "app/schemas/record%name.py\n::notice::forged"
    path = tmp_path / "comparison.json"
    path.write_text(json.dumps(_comparison(ahead_by=1, files=[malicious_name])), encoding="utf-8")

    result = drift_main(
        [
            "check",
            "--comparison",
            str(path),
            "--label",
            "label%value\n::warning::forged",
            "--max-commits",
            "10",
            "--surface-preset",
            "inflow-lab",
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "\n::notice::forged" not in output
    assert "\n::warning::forged" not in output
    assert "%25" in output
    assert "%0A" in output
    assert r"\n::notice::forged" in output


def test_summary_uses_noncolliding_code_spans_for_untrusted_backticks() -> None:
    malicious_name = "app/schemas/record`field``name.py"
    report = evaluate_drift(
        _comparison(ahead_by=1, files=[malicious_name]),
        max_commits=10,
        surface_patterns=("app/schemas/*",),
    )

    summary = render_summary(
        report,
        label="test pin",
        base_ref="base`ref",
        head_ref="head``ref",
        blocking=True,
    )

    assert "- Pinned commit: ``base`ref``" in summary
    assert "- Compared against: ```head``ref```" in summary
    assert "  - ```app/schemas/record`field``name.py```" in summary
    assert r"`app/schemas/record\`field\`\`name.py`" not in summary


@pytest.mark.parametrize(
    "payload",
    (
        "<img src=x onerror=alert(1)>",
        "![forged](https://attacker.invalid/collect)",
    ),
)
def test_summary_renders_untrusted_labels_and_reasons_as_inert_code(payload: str) -> None:
    malicious_name = f"app/{payload}.py"
    report = evaluate_drift(
        _comparison(ahead_by=1, files=[malicious_name]),
        max_commits=10,
        surface_patterns=("app/*",),
    )

    summary = render_summary(
        report,
        label=payload,
        base_ref="base",
        head_ref="head",
        blocking=True,
    )

    assert summary.splitlines()[0] == f"### Contract pin drift: `{payload}` -- FAILED"
    reason_line = next(line for line in summary.splitlines() if line.startswith("- Failed reason:"))
    assert reason_line == f"- Failed reason: `{report.reasons[0]}`"


def test_default_snapshot_path_is_the_file_the_pin_test_loads() -> None:
    assert DEFAULT_SNAPSHOT_PATH.exists()
    assert DEFAULT_SNAPSHOT_PATH.name == "regengine_required_kdes.json"

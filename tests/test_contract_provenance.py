"""Provenance of the pinned RegEngine contract, and the pin-drift check (#104).

Before #104, ``tests/test_regengine_contract_pin.py`` held a hand-typed
copy of RegEngine's required-KDE table under a docstring claiming it was
"copied verbatim from RegEngine". Both sides of that assertion lived in
this repo, so it could only ever prove this repo agreed with itself. The
claim was unfalsifiable by construction.

The vendored table is now generated from RegEngine's real source by
``scripts/regengine_kde_contract.py`` and carries its provenance as data:
which repository, file, symbol and commit it was read from, and a sha256
over the table itself. Two mechanisms make that data load-bearing rather
than decorative:

* offline, in this file: the recorded sha256 must match the file's own
  contents, so hand-editing the JSON instead of regenerating it fails;
* cross-repo, in the ``regengine-contract`` job of
  ``.github/workflows/ci.yml``: the table is re-extracted from a real
  RegEngine checkout and any difference fails the build.

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
from scripts.contract_pin_drift import (
    CONTRACT_SURFACE_PRESETS,
    DriftInputError,
    evaluate_drift,
    extract_pinned_ref,
    main as drift_main,
    matching_surface_files,
)
from scripts.regengine_kde_contract import (
    DEFAULT_SNAPSHOT_PATH,
    REGENGINE_ENUM_SYMBOL,
    REGENGINE_REPOSITORY,
    REGENGINE_SOURCE_PATH,
    REGENGINE_TABLE_SYMBOL,
    ContractSourceError,
    build_snapshot_document,
    extract_required_kdes,
    load_snapshot,
    table_sha256,
)


# Environment variable the cross-repo CI job sets so this file's optional
# "against the real upstream source" test runs there too. Locally:
#   REGENGINE_CHECKOUT=/path/to/regengine pytest tests/test_contract_provenance.py
REGENGINE_CHECKOUT_ENV = "REGENGINE_CHECKOUT"


UPSTREAM_FIXTURE = '''
from enum import Enum


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


def test_snapshot_records_where_upstream_actually_keeps_the_table() -> None:
    provenance = load_snapshot().provenance

    assert provenance["source_repository"] == REGENGINE_REPOSITORY
    assert provenance["source_path"] == REGENGINE_SOURCE_PATH
    assert provenance["source_symbol"] == REGENGINE_TABLE_SYMBOL
    assert provenance["source_enum"] == REGENGINE_ENUM_SYMBOL
    assert provenance["extracted_by"] == "scripts/regengine_kde_contract.py"
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


def test_snapshot_document_round_trips_through_the_loader(tmp_path: Path) -> None:
    document = build_snapshot_document(
        extract_required_kdes(UPSTREAM_FIXTURE), source_commit="a" * 40, extracted_at="2026-01-01"
    )
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_snapshot(path)

    assert loaded.table == {
        "harvesting": ("traceability_lot_code", "harvest_date"),
        "shipping": ("traceability_lot_code", "ship_date"),
    }
    assert loaded.recorded_table_sha256 == table_sha256(loaded.table)


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
    """The same assertion the cross-repo CI job enforces, available to
    anyone who has both repos checked out locally."""
    from scripts.regengine_kde_contract import extract_from_checkout

    upstream = extract_from_checkout(Path(os.environ[REGENGINE_CHECKOUT_ENV]))

    assert load_snapshot().table == upstream


# ---------------------------------------------------------------------------
# Pin drift
# ---------------------------------------------------------------------------


def _comparison(*, ahead_by: int, files: list[str], total_commits: int | None = None) -> dict[str, Any]:
    commits = [{"sha": f"{index:040x}"} for index in range(ahead_by)]
    return {
        "status": "behind",
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


def test_surface_presets_cover_the_files_the_contract_job_gates_on() -> None:
    """These presets are the machine-readable half of the same statement
    the workflow's path filter makes; if they diverge the drift check stops
    watching what CI watches."""
    inflow = CONTRACT_SURFACE_PRESETS["inflow-lab"]
    assert matching_surface_files(
        ["app/regengine_client.py", "app/controller.py", "app/schemas/domain.py", "app/contract.py"],
        inflow,
    ) == ("app/contract.py", "app/controller.py", "app/regengine_client.py", "app/schemas/domain.py")
    assert matching_surface_files(["app/static/app.js", "README.md"], inflow) == ()

    regengine = CONTRACT_SURFACE_PRESETS["regengine"]
    assert matching_surface_files(
        ["services/ingestion/app/webhook_models.py", "services/ingestion/app/webhook_router_v2/routes.py"],
        regengine,
    ) == (
        "services/ingestion/app/webhook_models.py",
        "services/ingestion/app/webhook_router_v2/routes.py",
    )


def test_extract_pinned_ref_reads_the_sha_off_an_env_line() -> None:
    workflow = "env:\n  INFLOW_LAB_REF: 4651e1a8e3562e44eadf65a67935276d754325a3  # pragma: allowlist secret\n"

    assert extract_pinned_ref(workflow) == "4651e1a8e3562e44eadf65a67935276d754325a3"
    assert extract_pinned_ref('  INFLOW_LAB_REF: "abc1234"\n') == "abc1234"


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


def test_default_snapshot_path_is_the_file_the_pin_test_loads() -> None:
    assert DEFAULT_SNAPSHOT_PATH.exists()
    assert DEFAULT_SNAPSHOT_PATH.name == "regengine_required_kdes.json"

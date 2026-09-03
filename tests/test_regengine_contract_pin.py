"""Pin app.cte_rules.REQUIRED_KDES to RegEngine's live webhook contract.

The expected table below is copied verbatim from RegEngine
services/ingestion/app/webhook_models.py REQUIRED_KDES_BY_CTE (the source
of truth for live ingest validation). If RegEngine changes its required
KDEs, update BOTH this table and app/cte_rules.py — a mismatch here means
the mock validates differently from live RegEngine, which resurrects the
"green demo, failing live post" drift this pin exists to catch.

NOTE (§1.1330(b)(7)): The TRANSFORMATION entry below already includes
input_traceability_lot_codes and input_products as required, ahead of the
live RegEngine contract. Update the live contract and remove this note once
RegEngine's webhook_models.py is aligned.
"""

from __future__ import annotations

import textwrap
from datetime import date, timedelta

import pytest

from app.cte_rules import REQUIRED_KDES
from app.schemas.domain import CTEType
from scripts import contract_pin_check
from scripts.contract_pin_check import ContractPinFailure


REGENGINE_REQUIRED_KDES_BY_CTE: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "harvest_date",
        "location_name",
        "reference_document",
    ),
    CTEType.COOLING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "cooling_date",
        "location_name",
        "reference_document",
    ),
    CTEType.INITIAL_PACKING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "packing_date",
        "location_name",
        "reference_document",
        "harvester_business_name",
    ),
    CTEType.FIRST_LAND_BASED_RECEIVING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "landing_date",
        "receiving_location",
        "reference_document",
    ),
    CTEType.SHIPPING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "ship_date",
        "ship_from_location",
        "ship_to_location",
        "reference_document",
        "tlc_source_reference",
    ),
    CTEType.RECEIVING: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "receive_date",
        "receiving_location",
        "immediate_previous_source",
        "reference_document",
        "tlc_source_reference",
    ),
    CTEType.TRANSFORMATION: (
        "traceability_lot_code",
        "product_description",
        "quantity",
        "unit_of_measure",
        "transformation_date",
        "location_name",
        "reference_document",
        "input_traceability_lot_codes",
        "input_products",
    ),
}


def test_required_kdes_match_regengine_contract_exactly() -> None:
    assert REQUIRED_KDES == REGENGINE_REQUIRED_KDES_BY_CTE


def test_every_cte_type_has_required_kdes() -> None:
    assert set(REQUIRED_KDES) == set(CTEType)


# ---------------------------------------------------------------------------
# #140 -- the pin has to be re-confirmed against RegEngine periodically
# ---------------------------------------------------------------------------


def test_pin_has_been_confirmed_against_regengine_recently() -> None:
    """Fails on the day the pin goes stale, not on the day it breaks live.

    This is a deliberate time bomb: nothing else in this repo can notice a
    RegEngine-side change to REQUIRED_KDES_BY_CTE. When it trips, re-read
    RegEngine's table, reconcile, then move PIN_CONFIRMED_ON forward -- moving
    the date without doing the comparison defeats the guard entirely.
    """
    contract_pin_check.check_pin_freshness()


def test_the_staleness_guard_actually_trips() -> None:
    stale_day = (
        contract_pin_check.PIN_CONFIRMED_ON
        + timedelta(days=contract_pin_check.MAX_PIN_AGE_DAYS + 1)
    )
    with pytest.raises(ContractPinFailure, match="last confirmed"):
        contract_pin_check.check_pin_freshness(today=stale_day)


def test_the_staleness_guard_passes_on_its_final_day() -> None:
    last_good = (
        contract_pin_check.PIN_CONFIRMED_ON
        + timedelta(days=contract_pin_check.MAX_PIN_AGE_DAYS)
    )
    assert contract_pin_check.check_pin_freshness(today=last_good)


def test_confirmation_date_is_not_in_the_future() -> None:
    assert contract_pin_check.PIN_CONFIRMED_ON <= date.today()


# ---------------------------------------------------------------------------
# #104 -- the cross-repo half, exercised against a fake RegEngine checkout
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_regengine(tmp_path):
    """A minimal RegEngine tree: the contract workflow and the KDE table."""

    def build(*, ref: str = "4651e1a8e3562e44eadf65a67935276d754325a3", table: str | None = None):
        workflow = tmp_path / contract_pin_check.REGENGINE_WORKFLOW
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(
            textwrap.dedent(
                f"""\
                name: Inflow Lab Contract CI
                env:
                  INFLOW_LAB_REF: {ref}
                """
            ),
            encoding="utf-8",
        )
        models = tmp_path / contract_pin_check.REGENGINE_WEBHOOK_MODELS
        models.parent.mkdir(parents=True, exist_ok=True)
        models.write_text(table if table is not None else _table_source(), encoding="utf-8")
        return tmp_path

    return build


def _table_source(overrides: dict[CTEType, tuple[str, ...]] | None = None) -> str:
    table = dict(REQUIRED_KDES)
    table.update(overrides or {})
    entries = "\n".join(
        f"    CTEType.{cte.name}: {tuple(kdes)!r}," for cte, kdes in table.items()
    )
    return f"REQUIRED_KDES_BY_CTE = {{\n{entries}\n}}\n"


def test_required_kdes_are_read_back_out_of_regengine(fake_regengine) -> None:
    path = fake_regengine()
    assert contract_pin_check.check_required_kdes(path)


def test_a_regengine_side_kde_change_fails_the_check(fake_regengine) -> None:
    """The drift this repo could not previously see at all."""
    drifted = _table_source(
        {CTEType.HARVESTING: REQUIRED_KDES[CTEType.HARVESTING] + ("grower_id",)}
    )
    path = fake_regengine(table=drifted)
    with pytest.raises(ContractPinFailure, match="HARVESTING"):
        contract_pin_check.check_required_kdes(path)


def test_the_pinned_ref_is_read_out_of_the_regengine_workflow(fake_regengine) -> None:
    path = fake_regengine(ref="0123456789abcdef0123456789abcdef01234567")
    assert (
        contract_pin_check.pinned_inflow_lab_ref(path)
        == "0123456789abcdef0123456789abcdef01234567"
    )


def test_pin_drift_fails_when_the_pinned_commit_is_unreachable(fake_regengine) -> None:
    """A pin naming a commit this checkout does not have cannot be measured."""
    path = fake_regengine(ref="0" * 40)
    with pytest.raises(ContractPinFailure, match="Could not measure drift"):
        contract_pin_check.check_pin_drift(path)


def test_drift_within_the_limit_passes(fake_regengine, monkeypatch) -> None:
    path = fake_regengine()
    monkeypatch.setattr(
        contract_pin_check, "commits_ahead_of", lambda ref: contract_pin_check.MAX_PIN_DRIFT_COMMITS
    )
    assert contract_pin_check.check_pin_drift(path)


def test_drift_past_the_limit_fails(fake_regengine, monkeypatch) -> None:
    path = fake_regengine()
    monkeypatch.setattr(
        contract_pin_check,
        "commits_ahead_of",
        lambda ref: contract_pin_check.MAX_PIN_DRIFT_COMMITS + 1,
    )
    with pytest.raises(ContractPinFailure, match="commits past"):
        contract_pin_check.check_pin_drift(path)


def test_staleness_only_run_exits_zero() -> None:
    """What CI runs on a tree with no RegEngine checkout available."""
    assert contract_pin_check.main([]) == 0


# ---------------------------------------------------------------------------
# #91 -- the reference doc's per-event field list vs. what actually goes out
# ---------------------------------------------------------------------------


def test_documented_wire_fields_match_the_client() -> None:
    """The skill reference once said RegEngine read input lots from `kdes`.

    It does not; it reads the top-level field only, and believing otherwise
    dropped every transformation lineage link on live ingest for months. This
    fails whenever the documented per-event shape and build_wire_body disagree.
    """
    contract_pin_check.check_documented_wire_fields()


def test_input_lot_codes_are_hoisted_to_the_top_level() -> None:
    baseline = contract_pin_check.emitted_event_fields(with_input_lots=False)
    with_lots = contract_pin_check.emitted_event_fields(with_input_lots=True)
    assert with_lots - baseline == {contract_pin_check.INPUT_LOT_CODES_FIELD}


def test_events_without_input_lots_omit_the_field_entirely() -> None:
    """Omitted, not an explicit null -- the wire shape must stay unchanged."""
    assert (
        contract_pin_check.INPUT_LOT_CODES_FIELD
        not in contract_pin_check.emitted_event_fields(with_input_lots=False)
    )


def test_the_wire_field_guard_trips_on_a_documentation_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(
        contract_pin_check, "documented_event_fields", lambda: {"cte_type"}
    )
    with pytest.raises(ContractPinFailure, match="does not match"):
        contract_pin_check.check_documented_wire_fields()

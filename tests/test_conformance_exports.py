"""Regulatory/standards conformance checks for the FDA and EPCIS exports."""

from __future__ import annotations

import pytest

import csv
import io
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from app.cte_rules import REQUIRED_KDES
from app.epcis_export import _BIZ_STEPS, render_epcis_document
from app.fda_export import FDA_EXPORT_COLUMNS, render_fda_request_csv
from app.schemas.domain import CTEType, RegEngineEvent, StoredEventRecord


# The complete CBV bizStep vocabulary (GS1 EPCIS 2.0 JSON-LD context).
CBV_BIZ_STEPS = {
    "accepting", "arriving", "assembling", "collecting", "commissioning",
    "consigning", "creating_class_instance", "cycle_counting", "decommissioning",
    "departing", "destroying", "disassembling", "dispensing", "encoding",
    "entering_exiting", "holding", "inspecting", "installing", "killing",
    "loading", "other", "packing", "picking", "receiving", "removing",
    "repackaging", "repairing", "replacing", "reserving", "retail_selling",
    "sampling", "sensor_reporting", "shipping", "staging_outbound",
    "stock_taking", "stocking", "storing", "transporting", "unloading",
    "unpacking", "void_shipping",
}

CBV_BIZ_STEP_PREFIX = "urn:epcglobal:cbv:bizstep:"


def _gln(location_name: str) -> str:
    return f"09521234{abs(hash(location_name)) % 100000:05d}"


def _record(
    cte_type: CTEType,
    *,
    lot_code: str = "TLC-CONF-001",
    location_name: str = "FreshPack Central",
    quantity: float = 100.0,
    unit_of_measure: str = "cases",
    timestamp: datetime | None = None,
    kdes: dict[str, Any] | None = None,
    parent_lot_codes: list[str] | None = None,
    sequence_no: int = 1,
) -> StoredEventRecord:
    return StoredEventRecord(
        sequence_no=sequence_no,
        payload_source="conformance-test",
        parent_lot_codes=parent_lot_codes or [],
        event=RegEngineEvent(
            cte_type=cte_type,
            traceability_lot_code=lot_code,
            product_description="Romaine Lettuce",
            quantity=quantity,
            unit_of_measure=unit_of_measure,
            location_name=location_name,
            timestamp=timestamp or datetime(2026, 2, 5, 14, 0, tzinfo=UTC),
            kdes=kdes or {},
        ),
    )


def _shipping_record(**overrides: Any) -> StoredEventRecord:
    kdes = {
        "ship_date": "2026-02-05",
        "ship_from_location": "FreshPack Central",
        "ship_to_location": "Distribution Center #4",
        "reference_document": "Bill of Lading BOL-CONF-001",
        "reference_document_type": "Bill of Lading",
        "reference_document_number": "BOL-CONF-001",
        "tlc_source_reference": "SRC-CONF-001",
    }
    kdes.update(overrides.pop("kdes", {}))
    return _record(CTEType.SHIPPING, kdes=kdes, **overrides)


def _receiving_record(**overrides: Any) -> StoredEventRecord:
    kdes = {
        "receive_date": "2026-02-05",
        "receiving_location": "Distribution Center #4",
        "immediate_previous_source": "FreshPack Central",
        "reference_document": "Bill of Lading BOL-CONF-001",
        "reference_document_type": "Bill of Lading",
        "reference_document_number": "BOL-CONF-001",
        "tlc_source_reference": "SRC-CONF-001",
    }
    kdes.update(overrides.pop("kdes", {}))
    overrides.setdefault("location_name", "Distribution Center #4")
    overrides.setdefault("timestamp", datetime(2026, 2, 5, 19, 15, tzinfo=UTC))
    return _record(CTEType.RECEIVING, kdes=kdes, **overrides)


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


# --- #186: FDA CSV carries both location descriptions and the TLC source ---


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_fda_export_keeps_regengine_mirrored_columns_first() -> None:
    assert FDA_EXPORT_COLUMNS[:11] == [
        "Traceability Lot Code",
        "Traceability Lot Code Description",
        "Product Description",
        "Quantity",
        "Unit of Measure",
        "Location Description",
        "Location Identifier (GLN)",
        "Date",
        "Time",
        "Reference Document Type",
        "Reference Document Number",
    ]


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_shipping_only_export_carries_ship_from_ship_to_and_tlc_source() -> None:
    csv_text = render_fda_request_csv([_shipping_record()], location_gln=_gln)
    (row,) = _rows(csv_text)

    assert row["Location Description"] == "FreshPack Central"
    assert row["Immediate Subsequent Recipient Location"] == "Distribution Center #4"
    assert row["Traceability Lot Code Source Reference"] == "SRC-CONF-001"


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_receiving_only_export_carries_receiver_previous_source_and_tlc_source() -> None:
    csv_text = render_fda_request_csv([_receiving_record()], location_gln=_gln)
    (row,) = _rows(csv_text)

    assert row["Location Description"] == "Distribution Center #4"
    assert row["Immediate Previous Source Location"] == "FreshPack Central"
    assert row["Traceability Lot Code Source Reference"] == "SRC-CONF-001"


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_export_surfaces_tlc_source_for_every_cte_that_requires_it() -> None:
    for cte_type, required in REQUIRED_KDES.items():
        if "tlc_source_reference" not in required:
            continue
        record = _record(cte_type, kdes={"tlc_source_reference": "SRC-CONF-XYZ"})
        (row,) = _rows(render_fda_request_csv([record], location_gln=_gln))
        assert row["Traceability Lot Code Source Reference"] == "SRC-CONF-XYZ", cte_type


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_export_accepts_alias_kdes_for_recipient_and_tlc_source() -> None:
    record = _shipping_record(
        kdes={
            "ship_to_location": "",
            "immediate_subsequent_recipient": "Retail DC #9",
            "tlc_source_reference": "",
            "traceability_lot_code_source_reference": "SRC-ALIAS-001",
        }
    )
    (row,) = _rows(render_fda_request_csv([record], location_gln=_gln))

    assert row["Immediate Subsequent Recipient Location"] == "Retail DC #9"
    assert row["Traceability Lot Code Source Reference"] == "SRC-ALIAS-001"


# --- #157: Date/Time normalized to UTC before splitting ---


def test_mixed_offset_events_render_distinct_utc_date_time() -> None:
    plus_five = timezone(timedelta(hours=5))
    records = [
        _shipping_record(
            lot_code="TLC-OFFSET-PLUS5",
            timestamp=datetime(2026, 2, 5, 23, 30, tzinfo=plus_five),
        ),
        _shipping_record(
            lot_code="TLC-OFFSET-UTC",
            timestamp=datetime(2026, 2, 5, 23, 30, tzinfo=UTC),
        ),
    ]
    rows = _rows(render_fda_request_csv(records, location_gln=_gln))

    assert (rows[0]["Date"], rows[0]["Time"]) == ("2026-02-05", "18:30:00")
    assert (rows[1]["Date"], rows[1]["Time"]) == ("2026-02-05", "23:30:00")
    assert (rows[0]["Date"], rows[0]["Time"]) != (rows[1]["Date"], rows[1]["Time"])


def test_naive_timestamps_are_treated_as_utc() -> None:
    record = _shipping_record(timestamp=datetime(2026, 2, 5, 9, 15))
    (row,) = _rows(render_fda_request_csv([record], location_gln=_gln))

    assert (row["Date"], row["Time"]) == ("2026-02-05", "09:15:00")


# --- #187: EPCIS sourceList / destinationList ---


def _render(records: list[StoredEventRecord]) -> list[dict[str, Any]]:
    document = render_epcis_document(records, source="conformance", location_gln=_gln)
    return document["epcisBody"]["eventList"]


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_shipping_event_emits_destination_list_for_subsequent_recipient() -> None:
    (event,) = _render([_shipping_record()])

    destinations = event["destinationList"]
    assert {entry["type"] for entry in destinations} == {
        "urn:epcglobal:cbv:sdt:location",
        "urn:epcglobal:cbv:sdt:owning_party",
        "urn:epcglobal:cbv:sdt:possessing_party",
    }
    assert all(entry["regengine:locationName"] == "Distribution Center #4" for entry in destinations)
    location_entry = next(
        entry for entry in destinations if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    assert location_entry["destination"] == "urn:regengine:location:Distribution%20Center%20%234"

    sources = event["sourceList"]
    source_entry = next(
        entry for entry in sources if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    assert source_entry["source"] == "urn:regengine:location:FreshPack%20Central"


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_receiving_event_emits_source_list_for_previous_source() -> None:
    (event,) = _render([_receiving_record()])

    sources = event["sourceList"]
    source_entry = next(
        entry for entry in sources if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    assert source_entry["source"] == "urn:regengine:location:FreshPack%20Central"
    assert source_entry["regengine:locationName"] == "FreshPack Central"

    destination_entry = next(
        entry
        for entry in event["destinationList"]
        if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    assert destination_entry["regengine:locationName"] == "Distribution Center #4"


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_shipping_receiving_pair_links_through_matching_sdt_locations() -> None:
    shipping, receiving = _render([_shipping_record(), _receiving_record()])

    ship_destination = next(
        entry
        for entry in shipping["destinationList"]
        if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    receive_source = next(
        entry
        for entry in receiving["sourceList"]
        if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    ship_source = next(
        entry
        for entry in shipping["sourceList"]
        if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )
    receive_destination = next(
        entry
        for entry in receiving["destinationList"]
        if entry["type"] == "urn:epcglobal:cbv:sdt:location"
    )

    assert ship_source["source"] == receive_source["source"]
    assert ship_destination["destination"] == receive_destination["destination"]
    # The extension blob is kept alongside the standard vocabulary, not replaced.
    assert shipping["regengine:kdes"]["ship_to_location"] == "Distribution Center #4"


def test_non_handoff_events_omit_source_and_destination_lists() -> None:
    (event,) = _render([_record(CTEType.HARVESTING, location_name="Sunrise Farm")])

    assert "sourceList" not in event
    assert "destinationList" not in event


# --- #188: bizStep vocabulary conformance ---


def test_cbv_prefixed_biz_steps_are_real_cbv_terms() -> None:
    for cte_type, biz_step in _BIZ_STEPS.items():
        if not biz_step.startswith(CBV_BIZ_STEP_PREFIX):
            continue
        term = biz_step[len(CBV_BIZ_STEP_PREFIX):]
        assert term in CBV_BIZ_STEPS, f"{cte_type} maps to non-CBV bizStep {biz_step}"


def test_transformation_biz_step_is_outside_the_cbv_namespace() -> None:
    biz_step = _BIZ_STEPS[CTEType.TRANSFORMATION]
    assert not biz_step.startswith("urn:epcglobal:cbv:")
    assert biz_step.startswith("urn:regengine:")


def test_every_cte_type_has_a_biz_step() -> None:
    assert set(_BIZ_STEPS) == set(CTEType)


# --- #159: EPCIS transformation input quantities ---


def _transformation_record(input_quantities: Any = None) -> StoredEventRecord:
    kdes: dict[str, Any] = {
        "transformation_date": "2026-02-07",
        "transformation_location": "ReadyFresh Processing Plant",
        "location_name": "ReadyFresh Processing Plant",
        "input_traceability_lot_codes": ["TLC-IN-1", "TLC-IN-2"],
        "input_products": ["Romaine Lettuce", "Spinach"],
        "reference_document": "Batch Record BATCH-CONF-001",
        "reference_document_type": "Batch Record",
        "reference_document_number": "BATCH-CONF-001",
        "batch_number": "BATCH-CONF-001",
    }
    if input_quantities is not None:
        kdes["input_quantities"] = input_quantities
    return _record(
        CTEType.TRANSFORMATION,
        lot_code="TLC-CONF-OUT",
        location_name="ReadyFresh Processing Plant",
        quantity=120.0,
        kdes=kdes,
    )


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_transformation_inputs_carry_per_lot_quantity_when_kdes_record_it() -> None:
    record = _transformation_record(
        [
            {"lot_code": "TLC-IN-1", "quantity": 80, "unit_of_measure": "cases"},
            {"lot_code": "TLC-IN-2", "quantity": 45.5, "unit_of_measure": "cases"},
        ]
    )
    (event,) = _render([record])

    by_lot = {
        element["regengine:traceabilityLotCode"]: element
        for element in event["inputQuantityList"]
    }
    assert by_lot["TLC-IN-1"]["quantity"] == 80.0
    assert by_lot["TLC-IN-1"]["uom"] == "cases"
    assert by_lot["TLC-IN-2"]["quantity"] == 45.5
    assert all("quantity" in element for element in event["inputQuantityList"])


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_transformation_inputs_accept_lot_to_quantity_mapping() -> None:
    record = _transformation_record({"TLC-IN-1": 80, "TLC-IN-2": 45.5})
    (event,) = _render([record])

    quantities = {
        element["regengine:traceabilityLotCode"]: element.get("quantity")
        for element in event["inputQuantityList"]
    }
    assert quantities == {"TLC-IN-1": 80.0, "TLC-IN-2": 45.5}


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_transformation_inputs_carry_product_description_from_input_products() -> None:
    (event,) = _render([_transformation_record()])

    by_lot = {
        element["regengine:traceabilityLotCode"]: element
        for element in event["inputQuantityList"]
    }
    assert by_lot["TLC-IN-1"]["regengine:productDescription"] == "Romaine Lettuce"
    assert by_lot["TLC-IN-2"]["regengine:productDescription"] == "Spinach"


def test_transformation_output_quantity_still_populated() -> None:
    (event,) = _render([_transformation_record()])

    (output,) = event["outputQuantityList"]
    assert output["quantity"] == 120.0
    assert output["uom"] == "cases"

"""Regulatory/standards conformance checks for the FDA and EPCIS exports."""

from __future__ import annotations


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


def test_fda_export_keeps_regengine_mirrored_columns_first() -> None:
    """The RegEngine-mirrored core comes first; #186's additions come after.

    Two corrections to what this test used to assert, both checked against
    primary sources rather than memory:

    * Index 1 is "Event Type (CTE)", NOT "Traceability Lot Code Description"
      (#94). That column held `event.cte_type.value`, which is a mislabel
      rather than a bad value: no KDE by that name exists anywhere in FSMA 204,
      and RegEngine's own canonical spreadsheet
      (services/compliance/app/fsma_spreadsheet.py) has no lot-code
      description column while it does have "Event Type (CTE)". Mirroring
      RegEngine -- which is what this test is named for -- means this column.
    * The mirrored core is 7 columns, not 11. #186 inserted the counterparty
      location and the TLC source reference after it, so asserting a flat
      11-wide prefix silently required those two NOT to exist. They are
      asserted in their own right below instead.
    """
    assert FDA_EXPORT_COLUMNS[:7] == [
        "Traceability Lot Code",
        "Event Type (CTE)",
        "Product Description",
        "Quantity",
        "Unit of Measure",
        "Location Description",
        "Location Identifier (GLN)",
    ]
    # #186's two additions sit between the mirrored core and the document
    # columns, so a Shipping- or Receiving-only export carries the whole
    # required location pair.
    assert FDA_EXPORT_COLUMNS[7:9] == [
        "Ship-To / Previous Source Location Description",
        "TLC Source Reference",
    ]
    assert FDA_EXPORT_COLUMNS[9:] == [
        "Date",
        "Time",
        "Reference Document Type",
        "Reference Document Number",
    ]


def test_shipping_only_export_carries_ship_from_ship_to_and_tlc_source() -> None:
    csv_text = render_fda_request_csv([_shipping_record()], location_gln=_gln)
    (row,) = _rows(csv_text)

    assert row["Location Description"] == "FreshPack Central"
    assert row["Ship-To / Previous Source Location Description"] == "Distribution Center #4"
    assert row["TLC Source Reference"] == "SRC-CONF-001"


def test_receiving_only_export_carries_receiver_previous_source_and_tlc_source() -> None:
    csv_text = render_fda_request_csv([_receiving_record()], location_gln=_gln)
    (row,) = _rows(csv_text)

    assert row["Location Description"] == "Distribution Center #4"
    assert row["Ship-To / Previous Source Location Description"] == "FreshPack Central"
    assert row["TLC Source Reference"] == "SRC-CONF-001"


def test_export_surfaces_tlc_source_for_every_cte_that_requires_it() -> None:
    for cte_type, required in REQUIRED_KDES.items():
        if "tlc_source_reference" not in required:
            continue
        record = _record(cte_type, kdes={"tlc_source_reference": "SRC-CONF-XYZ"})
        (row,) = _rows(render_fda_request_csv([record], location_gln=_gln))
        assert row["TLC Source Reference"] == "SRC-CONF-XYZ", cte_type


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

    assert row["Ship-To / Previous Source Location Description"] == "Retail DC #9"
    assert row["TLC Source Reference"] == "SRC-ALIAS-001"


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


def test_shipping_event_emits_destination_list_for_subsequent_recipient() -> None:
    """Shipping's ship-to becomes a CBV destinationList entry (#187).

    Two things about the shape are deliberate and both post-date the version
    this test was originally written against:

    * ``location`` only. CBV also defines ``owning_party`` and
      ``possessing_party``, but this app has no ownership or possession model
      distinct from location, so emitting those would assert a handoff it does
      not model.
    * The BARE token, not ``urn:epcglobal:cbv:sdt:location``. GS1's
      epcis-context.jsonld declares this member as ``"@type": "@vocab"`` over
      exactly three short names, so only a token expands to ``cbv:SDT-location``;
      and the official EPCIS 2.0 JSON Schema's ``source-dest-type`` rejects the
      ``urn:epcglobal:cbv`` prefix outright via a negative lookahead. Both
      verified against the published context and schema, not from memory.
    """
    (event,) = _render([_shipping_record()])

    destinations = event["destinationList"]
    assert {entry["type"] for entry in destinations} == {"location"}
    (destination,) = destinations
    assert destination["destination"] == "urn:regengine:location:Distribution%20Center%20%234"
    # The name is recoverable from the URI itself, so no separate
    # regengine:locationName member is emitted alongside it.
    assert set(destination) == {"type", "destination"}


def test_receiving_event_emits_source_list_for_previous_source() -> None:
    (event,) = _render([_receiving_record()])

    sources = event["sourceList"]
    assert {entry["type"] for entry in sources} == {"location"}
    (source,) = sources
    assert source["source"] == "urn:regengine:location:FreshPack%20Central"
    assert set(source) == {"type", "source"}


def test_source_and_destination_types_use_gs1s_declared_vocabulary_tokens() -> None:
    """Pinned as set membership, so this fails for ANY urn, not just one.

    Emitting the otherwise-legitimate ``urn:epcglobal:cbv:sdt:location`` alias
    made every shipping and receiving document schema-invalid, which is the
    opposite of what #187 asked for. A literal-equality assertion would go on
    passing if the value drifted to some other URN, so this asserts membership
    in GS1's three declared tokens instead.
    """
    declared_tokens = {"owning_party", "possessing_party", "location"}

    seen = 0
    for event in _render([_shipping_record(), _receiving_record()]):
        for entry in event.get("sourceList", []) + event.get("destinationList", []):
            seen += 1
            assert entry["type"] in declared_tokens, (
                f"{entry['type']!r} is not one of GS1's declared sourceDestinationType "
                "tokens; a URN here fails both JSON-LD expansion and the EPCIS 2.0 schema"
            )
    assert seen, "no source/destination entries were produced to check"


def test_a_shipping_receiving_pair_names_each_others_locations() -> None:
    """The handoff is traceable across the pair, one side at a time.

    What #187 asks for is that a Shipping/Receiving pair link through matching
    location identifiers. It is only half met, and this test says which half:
    Shipping carries a destinationList naming where the lot went, and
    Receiving carries a sourceList naming where it came from -- so the two
    endpoints of the handoff ARE both in the document and both as CBV
    entries.

    What is NOT emitted is the other side of each event: Shipping has no
    sourceList naming the shipper, and Receiving no destinationList naming
    the receiver. Those are each event's own ``bizLocation``, so the
    information is present in the document, just not duplicated into a CBV
    list. A consumer that links purely on sourceList/destinationList
    therefore has to fall back on bizLocation for one end of each hop.
    """
    shipping, receiving = _render([_shipping_record(), _receiving_record()])

    (ship_destination,) = shipping["destinationList"]
    (receive_source,) = receiving["sourceList"]

    assert ship_destination["destination"] == "urn:regengine:location:Distribution%20Center%20%234"
    assert receive_source["source"] == "urn:regengine:location:FreshPack%20Central"

    # The un-emitted halves, asserted so a future change that adds them fails
    # here and gets to update this docstring rather than silently widening it.
    assert "sourceList" not in shipping
    assert "destinationList" not in receiving
    assert shipping["bizLocation"] and receiving["bizLocation"]


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


# ---------------------------------------------------------------------------
# #188 bizStep vocabulary, handoff-list scoping, and per-input quantities
# ---------------------------------------------------------------------------


def test_cbv_prefixed_biz_steps_are_real_cbv_terms() -> None:
    for cte_type, biz_step in _BIZ_STEPS.items():
        if not biz_step.startswith(CBV_BIZ_STEP_PREFIX):
            continue
        term = biz_step[len(CBV_BIZ_STEP_PREFIX):]
        assert term in CBV_BIZ_STEPS, f"{cte_type} maps to non-CBV bizStep {biz_step}"


def test_every_cte_type_has_a_biz_step() -> None:
    assert set(_BIZ_STEPS) == set(CTEType)


def test_transformation_biz_step_is_outside_the_cbv_namespace() -> None:
    biz_step = _BIZ_STEPS[CTEType.TRANSFORMATION]
    assert not biz_step.startswith("urn:epcglobal:cbv:")
    assert biz_step.startswith("urn:regengine:")


def test_non_handoff_events_omit_source_and_destination_lists() -> None:
    (event,) = _render([_record(CTEType.HARVESTING, location_name="Sunrise Farm")])

    assert "sourceList" not in event
    assert "destinationList" not in event


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


def test_transformation_inputs_accept_lot_to_quantity_mapping() -> None:
    record = _transformation_record({"TLC-IN-1": 80, "TLC-IN-2": 45.5})
    (event,) = _render([record])

    quantities = {
        element["regengine:traceabilityLotCode"]: element.get("quantity")
        for element in event["inputQuantityList"]
    }
    assert quantities == {"TLC-IN-1": 80.0, "TLC-IN-2": 45.5}



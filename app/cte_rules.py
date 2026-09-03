from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .scenarios import ScenarioPreset
from .schemas.domain import CTEType, RegEngineEvent, StoredEventRecord

# Warning tiers, in descending order of how much they should worry a viewer.
# "required" means the record is missing something the rule actually demands;
# "recommended" means it is missing something that makes the record more useful
# but that no rule requires.
SEVERITY_REQUIRED = "required"
SEVERITY_RECOMMENDED = "recommended"


#: A warning is either a gap that would fail live RegEngine ingest ("required")
#: or a nice-to-have the audit lens would like to see ("recommended"). Before
#: this existed the distinction lived only inside the prose of `message`, so no
#: surface could tell a blocker from a suggestion.
WarningSeverity = Literal["required", "recommended"]

#: Sort key: required warnings come first wherever a list is rendered, so the
#: gaps that would fail live ingest are what an operator reads first.
SEVERITY_ORDER: dict[str, int] = {"required": 0, "recommended": 1}


@dataclass(frozen=True, slots=True)
class CTEValidationWarning:
    field: str
    message: str
<<<<<<< HEAD
    # Defaulted so every existing construction site keeps working; the tiers
    # that matter set it explicitly.
    severity: str = SEVERITY_RECOMMENDED
    # The regulation the requirement comes from, when there is one to cite.
    citation: str | None = None
=======
    severity: WarningSeverity = "recommended"
>>>>>>> origin/main


@dataclass(frozen=True, slots=True)
class EventRequirement:
    field: str
    message: str
    cte_types: tuple[CTEType, ...]
    any_of: tuple[str, ...] = ()
    expected_value: str | None = None


@dataclass(frozen=True, slots=True)
class AuditCheckDefinition:
    label: str
    detail: str
    cte_types: tuple[CTEType, ...] = ()
    any_kdes: tuple[str, ...] = ()
    exact_field: str | None = None
    exact_value: str | None = None
    reference_document_prefix: str | None = None
    forbidden_cte_types: tuple[CTEType, ...] = ()


# Mirrors RegEngine's canonical ingest contract. Top-level IngestEvent fields
# are merged with the KDE dict before checking, matching how the validator
# resolves required keys on the wire.
REQUIRED_KDES: dict[CTEType, tuple[str, ...]] = {
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
    # Matches RegEngine webhook_models.REQUIRED_KDES_BY_CTE exactly
    # (§1.1325(c)(7)); vessel/receiver detail is recommended, not required.
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
    # §1.1330(b)(7): input lot codes and products are required by regulation for
    # transformation records. This anticipates the RegEngine contract pin being
    # updated once the live webhook_models.py aligns.
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

# KDEs the FSMA 204 rule requires that RegEngine's *ingest-time* schema gate
# (REQUIRED_KDES above, pinned verbatim to its REQUIRED_KDES_BY_CTE) does not
# check. They are not a divergence from RegEngine: RegEngine enforces these
# through its compliance rules engine instead, where "Transformation events must
# list all input traceability lot codes" is seeded at `critical` severity citing
# 21 CFR 1.1350(a)(4) (services/shared/rules/seeds.py). Filing them under
# RECOMMENDED_KDES understated them as a soft nudge, which is what #189 is
# about; promoting them into REQUIRED_KDES would instead break the ingest pin
# and claim live ingest rejects what it accepts. Hence a third tier: reported at
# required severity, kept out of the pinned table.
FSMA_REQUIRED_KDES: dict[CTEType, tuple[tuple[str, str], ...]] = {
    CTEType.TRANSFORMATION: (
        ("input_traceability_lot_codes", "21 CFR 1.1350(a)(4)"),
        ("input_products", "21 CFR 1.1350(a)(5)"),
        ("input_quantities", "21 CFR 1.1350(a)(6)"),
    ),
}

RECOMMENDED_KDES: dict[CTEType, tuple[str, ...]] = {
    CTEType.HARVESTING: ("field_name", "tlc_source_reference"),
    CTEType.COOLING: ("harvest_location", "tlc_source_reference"),
    CTEType.INITIAL_PACKING: ("farm_location", "tlc_source_reference"),
    CTEType.FIRST_LAND_BASED_RECEIVING: (
        "first_land_based_receiver",
        "vessel_identifier",
        "vessel_name",
        "water_temperature_c",
        "reference_document_number",
        "tlc_source_reference",
    ),
    CTEType.SHIPPING: ("carrier", "reference_document_type"),
    CTEType.RECEIVING: ("reference_document_type",),
<<<<<<< HEAD
    CTEType.TRANSFORMATION: ("reference_document_type",),
=======
    CTEType.TRANSFORMATION: ("input_quantities", "reference_document_type"),
>>>>>>> origin/main
}

INDUSTRY_EVENT_REQUIREMENTS: dict[str, tuple[EventRequirement, ...]] = {
    "produce": (
        EventRequirement(
            field="field_gps_coordinates",
            message="Produce harvest flows should include field_gps_coordinates",
            cte_types=(CTEType.HARVESTING,),
        ),
        EventRequirement(
            field="packaging_hierarchy",
            message="Produce packout and transformation flows should include packaging_hierarchy",
            cte_types=(CTEType.INITIAL_PACKING, CTEType.TRANSFORMATION),
        ),
    ),
    "seafood": (
        EventRequirement(
            field="vessel_identifier",
            message="Seafood first receiver flows should include vessel_identifier",
            cte_types=(CTEType.FIRST_LAND_BASED_RECEIVING,),
        ),
        EventRequirement(
            field="landing_date",
            message="Seafood first receiver flows should include landing_date",
            cte_types=(CTEType.FIRST_LAND_BASED_RECEIVING,),
        ),
    ),
    "dairy": (
        EventRequirement(
            field="flow_type",
            message="Dairy packout should preserve flow_type=continuous",
            cte_types=(CTEType.INITIAL_PACKING,),
            expected_value="continuous",
        ),
        EventRequirement(
            field="silo_identifier",
            message="Dairy packout should include silo_identifier or vat_identifier",
            cte_types=(CTEType.INITIAL_PACKING,),
            any_of=("silo_identifier", "vat_identifier"),
        ),
    ),
}

INDUSTRY_AUDIT_CHECKS: dict[str, tuple[AuditCheckDefinition, ...]] = {
    "produce": (
        AuditCheckDefinition(
            label="Field coordinates",
            detail="Leafy greens and produce should carry field GPS to strengthen origin traceability.",
            any_kdes=("field_gps_coordinates",),
        ),
        AuditCheckDefinition(
            label="PLU-linked product identity",
            detail="Retail and produce scenarios should surface PLU or similar downstream item identifiers.",
            any_kdes=("plu_code",),
        ),
        AuditCheckDefinition(
            label="Packout hierarchy",
            detail="Expect bulk to clamshell to master-case metadata in pack and transformation events.",
            any_kdes=("packaging_conversion", "packaging_hierarchy"),
        ),
    ),
    "seafood": (
        AuditCheckDefinition(
            label="Vessel-linked receiving",
            detail="Expect vessel identifier and landing date on first land-based receiving records.",
            cte_types=(CTEType.FIRST_LAND_BASED_RECEIVING,),
            any_kdes=("vessel_identifier", "landing_date"),
        ),
        AuditCheckDefinition(
            label="GS1 dock references",
            detail="Shipping and receiving should reuse a GS1-128 SSCC-style document reference.",
            reference_document_prefix="GS1-128 (00)",
        ),
        AuditCheckDefinition(
            label="Packaging step-up",
            detail="Expect tote to fillet bag to master case hierarchy to show downstream packout realism.",
            any_kdes=("packaging_hierarchy",),
        ),
    ),
    "dairy": (
        AuditCheckDefinition(
            label="Continuous flow KDEs",
            detail="Look for silo or vat identifiers rather than discrete produce-style field-only records.",
            exact_field="flow_type",
            exact_value="continuous",
        ),
        AuditCheckDefinition(
            label="Cooling bypass",
            detail="This scenario should avoid produce-specific cooling steps.",
            forbidden_cte_types=(CTEType.COOLING,),
        ),
        AuditCheckDefinition(
            label="Container lineage",
            detail="Audit trail should preserve the equipment containers that define the lot stream.",
            any_kdes=("silo_identifier", "vat_identifier"),
        ),
    ),
}


def validate_event_kdes(event: RegEngineEvent) -> list[CTEValidationWarning]:
    warnings: list[CTEValidationWarning] = []
    available = merged_event_values(event)

    required = REQUIRED_KDES.get(event.cte_type, ())
    for field in required:
        if not _has_value(available.get(field)):
            warnings.append(
                CTEValidationWarning(
                    field=field,
                    message=f"Missing expected {event.cte_type.value} KDE: {field}",
<<<<<<< HEAD
                    severity=SEVERITY_REQUIRED,
                )
            )

    for field, citation in FSMA_REQUIRED_KDES.get(event.cte_type, ()):
        if not _has_value(available.get(field)):
            warnings.append(
                CTEValidationWarning(
                    field=field,
                    message=(
                        f"Missing expected {event.cte_type.value} KDE: {field} "
                        f"(required by {citation})"
                    ),
                    severity=SEVERITY_REQUIRED,
                    citation=citation,
=======
                    severity="required",
>>>>>>> origin/main
                )
            )

    for field in RECOMMENDED_KDES.get(event.cte_type, ()):
        if not _has_value(available.get(field)):
            warnings.append(
                CTEValidationWarning(
                    field=field,
                    message=f"Missing recommended {event.cte_type.value} KDE: {field}",
                    severity="recommended",
                )
            )

    if event.cte_type == CTEType.TRANSFORMATION:
        input_lots = available.get("input_traceability_lot_codes")
        if _has_value(input_lots) and not _is_nonempty_string_list(input_lots):
            warnings.append(
                CTEValidationWarning(
                    field="input_traceability_lot_codes",
                    message="Transformation input_traceability_lot_codes should be a non-empty list of lot codes",
                    severity=SEVERITY_REQUIRED,
                    citation="21 CFR 1.1350(a)(4)",
                )
            )

        input_quantities = available.get("input_quantities")
        if _has_value(input_quantities) and not _is_input_quantity_list(input_quantities):
            warnings.append(
                CTEValidationWarning(
                    field="input_quantities",
                    message=(
                        "Transformation input_quantities should be a list of "
                        "{lot_code, quantity, unit_of_measure} entries, one per input lot"
                    ),
                    severity=SEVERITY_REQUIRED,
                    citation="21 CFR 1.1350(a)(6)",
                )
            )

    return sort_warnings(warnings)


def audit_warnings_for_event(event: RegEngineEvent, scenario: ScenarioPreset) -> list[CTEValidationWarning]:
    warnings = list(validate_event_kdes(event))
    available = merged_event_values(event)
    reference_document = str(available.get("reference_document") or "")

    if scenario.reference_format == "GS1" and reference_document and not reference_document.startswith("GS1"):
        warnings.append(
            CTEValidationWarning(
                field="reference_document",
                message="Reference document should use a GS1-linked format",
            )
        )

    for requirement in INDUSTRY_EVENT_REQUIREMENTS.get(scenario.industry_type, ()):
        if event.cte_type not in requirement.cte_types:
            continue
        if requirement.expected_value is not None:
            if available.get(requirement.field) != requirement.expected_value:
                warnings.append(
                    CTEValidationWarning(field=requirement.field, message=requirement.message)
                )
            continue
        if requirement.any_of:
            if not any(_has_value(available.get(field)) for field in requirement.any_of):
                warnings.append(
                    CTEValidationWarning(field=requirement.field, message=requirement.message)
                )
            continue
        if not _has_value(available.get(requirement.field)):
            warnings.append(
                CTEValidationWarning(field=requirement.field, message=requirement.message)
            )

    return dedupe_warnings(warnings)


def evaluate_audit_checks(
    records: list[StoredEventRecord],
    scenario: ScenarioPreset,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for definition in INDUSTRY_AUDIT_CHECKS.get(scenario.industry_type, ()):
        ok = _evaluate_check(records, definition)
        checks.append(
            {
                "label": definition.label,
                "ok": ok,
                "detail": definition.detail,
            }
        )
    return checks


def merged_event_values(event: RegEngineEvent) -> dict[str, Any]:
    available: dict[str, Any] = {
        "location_name": event.location_name,
        "location_gln": event.location_gln,
        "traceability_lot_code": event.traceability_lot_code,
        "product_description": event.product_description,
        "quantity": event.quantity,
        "unit_of_measure": event.unit_of_measure,
        **event.kdes,
    }
    tlc_reference = available.get("tlc_source_reference") or available.get(
        "traceability_lot_code_source_reference"
    )
    if _has_value(tlc_reference):
        available.setdefault("tlc_source_reference", tlc_reference)
        available.setdefault("traceability_lot_code_source_reference", tlc_reference)
    if event.cte_type == CTEType.FIRST_LAND_BASED_RECEIVING:
        receiver = available.get("receiving_location") or available.get("first_land_based_receiver") or event.location_name
        available.setdefault("receiving_location", receiver)
        available.setdefault("first_land_based_receiver", receiver)
    return available


def dedupe_warnings(warnings: list[CTEValidationWarning]) -> list[CTEValidationWarning]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CTEValidationWarning] = []
    for warning in warnings:
        key = (warning.field, warning.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return sort_warnings(deduped)


def sort_warnings(warnings: list[CTEValidationWarning]) -> list[CTEValidationWarning]:
    """Required-severity warnings first, otherwise stable in discovery order."""
    return sorted(warnings, key=lambda warning: SEVERITY_ORDER.get(warning.severity, 1))


def _evaluate_check(records: list[StoredEventRecord], definition: AuditCheckDefinition) -> bool:
    if definition.forbidden_cte_types:
        return not any(record.event.cte_type in definition.forbidden_cte_types for record in records)

    for record in records:
        event = record.event
        if definition.cte_types and event.cte_type not in definition.cte_types:
            continue
        available = merged_event_values(event)
        if definition.reference_document_prefix:
            if str(available.get("reference_document") or "").startswith(definition.reference_document_prefix):
                return True
            continue
        if definition.exact_field and definition.exact_value is not None:
            if available.get(definition.exact_field) == definition.exact_value:
                return True
            continue
        if definition.any_kdes:
            if any(_has_value(available.get(field)) for field in definition.any_kdes):
                return True
            continue
    return False


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def _is_nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.strip() for item in value
    )


def _is_input_quantity_list(value: Any) -> bool:
    """One {lot_code, quantity, unit_of_measure} entry per input lot.

    21 CFR 1.1350(a)(6) requires the quantity and unit of measure consumed
    *from each input lot*, not an aggregate -- so a single total, or a yield
    ratio, does not satisfy it.
    """
    if not isinstance(value, list) or not value:
        return False
    for entry in value:
        if not isinstance(entry, dict):
            return False
        lot_code = entry.get("lot_code")
        quantity = entry.get("quantity")
        unit = entry.get("unit_of_measure")
        if not isinstance(lot_code, str) or not lot_code.strip():
            return False
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
            return False
        if not isinstance(unit, str) or not unit.strip():
            return False
    return True

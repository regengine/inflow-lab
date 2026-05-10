from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .cte_rules import CTEValidationWarning, validate_event_kdes
from .scenarios import ScenarioPreset
from .schemas.domain import StoredEventRecord


def summarize_scenario_audit(
    records: list[StoredEventRecord],
    scenario: ScenarioPreset,
) -> dict[str, Any]:
    checks = _scenario_checks(records, scenario)
    passed = sum(1 for check in checks if check["ok"])
    total = len(checks) or 1
    score = round((passed / total) * 100)
    tone = "watch"
    label = "Developing"
    if score >= 85:
        tone = "ready"
        label = "Audit-ready pattern"
    elif score >= 60:
        tone = "progress"
        label = "Good signal coverage"

    warnings_by_record: dict[str, list[dict[str, str]]] = {}
    total_warning_count = 0
    for record in records:
        warning_payload = [
            asdict(warning)
            for warning in audit_warnings_for_record(record, scenario)
        ]
        if warning_payload:
            warnings_by_record[record.record_id] = warning_payload
            total_warning_count += len(warning_payload)

    return {
        "industry_type": scenario.industry_type,
        "reference_format": scenario.reference_format,
        "requires_cooling": scenario.requires_cooling,
        "score": score,
        "tone": tone,
        "label": label,
        "passed": passed,
        "total": total,
        "missing": total - passed,
        "checks": checks,
        "warning_count": total_warning_count,
        "records_with_warnings": len(warnings_by_record),
        "warnings_by_record": warnings_by_record,
    }


def audit_warnings_for_record(
    record: StoredEventRecord,
    scenario: ScenarioPreset,
) -> list[CTEValidationWarning]:
    event = record.event
    warnings = list(validate_event_kdes(event))
    kdes = event.kdes or {}
    reference_document = str(kdes.get("reference_document") or "")

    if scenario.reference_format == "GS1" and reference_document and not reference_document.startswith("GS1"):
        warnings.append(
            CTEValidationWarning(
                field="reference_document",
                message="Reference document should use a GS1-linked format",
            )
        )

    if scenario.industry_type == "seafood":
        if event.cte_type.value == "first_land_based_receiving" and not _has_value(kdes.get("vessel_identifier")):
            warnings.append(
                CTEValidationWarning(
                    field="vessel_identifier",
                    message="Seafood first receiver flows should include vessel_identifier",
                )
            )
        if event.cte_type.value == "first_land_based_receiving" and not _has_value(kdes.get("landing_date")):
            warnings.append(
                CTEValidationWarning(
                    field="landing_date",
                    message="Seafood first receiver flows should include landing_date",
                )
            )
    elif scenario.industry_type == "dairy":
        if event.cte_type.value == "initial_packing" and kdes.get("flow_type") != "continuous":
            warnings.append(
                CTEValidationWarning(
                    field="flow_type",
                    message="Dairy packout should preserve flow_type=continuous",
                )
            )
        if event.cte_type.value == "initial_packing" and not (
            _has_value(kdes.get("silo_identifier")) or _has_value(kdes.get("vat_identifier"))
        ):
            warnings.append(
                CTEValidationWarning(
                    field="silo_identifier",
                    message="Dairy packout should include silo_identifier or vat_identifier",
                )
            )
    else:
        if event.cte_type.value == "harvesting" and not _has_value(kdes.get("field_gps_coordinates")):
            warnings.append(
                CTEValidationWarning(
                    field="field_gps_coordinates",
                    message="Produce harvest flows should include field_gps_coordinates",
                )
            )
        if event.cte_type.value in {"initial_packing", "transformation"} and not _has_value(
            kdes.get("packaging_hierarchy")
        ):
            warnings.append(
                CTEValidationWarning(
                    field="packaging_hierarchy",
                    message="Produce packout and transformation flows should include packaging_hierarchy",
                )
            )

    return _dedupe_warnings(warnings)


def _scenario_checks(records: list[StoredEventRecord], scenario: ScenarioPreset) -> list[dict[str, Any]]:
    def has_kde(cte_type: str, key: str) -> bool:
        return any(
            record.event.cte_type.value == cte_type and _has_value(record.event.kdes.get(key))
            for record in records
        )

    def has_any(key: str) -> bool:
        return any(_has_value(record.event.kdes.get(key)) for record in records)

    if scenario.industry_type == "seafood":
        return [
            {
                "label": "Vessel-linked receiving",
                "ok": has_kde("first_land_based_receiving", "vessel_identifier"),
                "detail": "Expect vessel identifier and landing date on first land-based receiving records.",
            },
            {
                "label": "GS1 dock references",
                "ok": any(
                    str(record.event.kdes.get("reference_document") or "").startswith("GS1-128 (00)")
                    for record in records
                ),
                "detail": "Shipping and receiving should reuse a GS1-128 SSCC-style document reference.",
            },
            {
                "label": "Packaging step-up",
                "ok": has_any("packaging_hierarchy"),
                "detail": "Expect tote to fillet bag to master case hierarchy to show downstream packout realism.",
            },
        ]

    if scenario.industry_type == "dairy":
        return [
            {
                "label": "Continuous flow KDEs",
                "ok": any(record.event.kdes.get("flow_type") == "continuous" for record in records),
                "detail": "Look for silo or vat identifiers rather than discrete produce-style field-only records.",
            },
            {
                "label": "Cooling bypass",
                "ok": not any(record.event.cte_type.value == "cooling" for record in records),
                "detail": "This scenario should avoid produce-specific cooling steps.",
            },
            {
                "label": "Container lineage",
                "ok": has_any("silo_identifier") or has_any("vat_identifier"),
                "detail": "Audit trail should preserve the equipment containers that define the lot stream.",
            },
        ]

    return [
        {
            "label": "Field coordinates",
            "ok": has_any("field_gps_coordinates"),
            "detail": "Leafy greens and produce should carry field GPS to strengthen origin traceability.",
        },
        {
            "label": "PLU-linked product identity",
            "ok": has_any("plu_code"),
            "detail": "Retail and produce scenarios should surface PLU or similar downstream item identifiers.",
        },
        {
            "label": "Packout hierarchy",
            "ok": has_any("packaging_conversion") or has_any("packaging_hierarchy"),
            "detail": "Expect bulk to clamshell to master-case metadata in pack and transformation events.",
        },
    ]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def _dedupe_warnings(warnings: list[CTEValidationWarning]) -> list[CTEValidationWarning]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CTEValidationWarning] = []
    for warning in warnings:
        key = (warning.field, warning.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped

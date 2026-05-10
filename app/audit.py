from __future__ import annotations

from dataclasses import asdict

from .cte_rules import CTEValidationWarning, audit_warnings_for_event, evaluate_audit_checks
from .scenarios import ScenarioPreset
from .schemas.domain import StoredEventRecord


def summarize_scenario_audit(
    records: list[StoredEventRecord],
    scenario: ScenarioPreset,
) -> dict[str, Any]:
    checks = evaluate_audit_checks(records, scenario)
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
    return audit_warnings_for_event(record.event, scenario)

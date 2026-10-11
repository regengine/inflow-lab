"""Jev follow-up recommendation for simulated import batches.

inflow-lab is a mock-first FSMA 204 simulator: the safest place to deploy a
Jev judgment first, because the stakes are zero and it mirrors the customer
environment where RegEngine would later use the same pattern.

The repo already scores batches deterministically
(`audit.summarize_scenario_audit`). What it lacks is a *judgment* about what
the operator should do next. This module adds one atomic Jev Choice: given
batch statistics computed in code, recommend the operator's next action.
The recommendation rides in the response passthrough dict; it never changes
import, store, or delivery behavior.

Fail-open: no TYPESAFE_API_KEY, transport error, malformed answer, or
sub-threshold confidence means "no recommendation" (None). The import
response is unchanged.

Data boundary: the state carries aggregate statistics only (fractions,
counts, CTE type names). No lot codes, quantities, names, or other event
values are transmitted to api.typesafe.ai.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import urllib.request

from .cte_rules import validate_event_kdes
from .schemas.domain import RegEngineEvent

logger = logging.getLogger(__name__)

_JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
_JEV_API_KEY_ENV = "TYPESAFE_API_KEY"
_JEV_MODEL = "jev-latest"
_JEV_TIMEOUT_S = 10.0

# Minimum confidence for a recommendation to be surfaced. Below this the
# judgment is discarded (fail-open). Starting point — validate and adjust.
JEV_MIN_CONFIDENCE = 0.6

# Symmetric option descriptions: each states what the option covers, in
# parallel structure, without arguing for any of them.
_FOLLOWUP_OPTIONS = {
    "proceed": "The batch looks healthy; the operator can proceed with the demo or simulation.",
    "fix_required_warnings": "Some events have data gaps; the operator should review and fix them before relying on this batch.",
    "regenerate": "Batch quality is too low to be useful; the operator should regenerate or re-import the batch.",
}

_FOLLOWUP_INSTRUCTIONS = (
    "Given the import batch statistics in the state, choose the single best "
    "next action for the operator from the closed set in `options`. "
    "`frac_with_lot_code` and `frac_kde_complete` are fractions from 0 to 1; "
    "`lineage_links` counts events linked to prior lots."
)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep the Typesafe bearer credential on its fixed origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _as_regengine_event(event: object) -> RegEngineEvent:
    """Normalize documented event-like inputs before canonical validation."""
    if isinstance(event, RegEngineEvent):
        return event
    return RegEngineEvent(
        cte_type=getattr(event, "cte_type"),
        traceability_lot_code=getattr(event, "traceability_lot_code"),
        product_description=getattr(event, "product_description"),
        quantity=getattr(event, "quantity"),
        unit_of_measure=getattr(event, "unit_of_measure"),
        location_name=getattr(event, "location_name"),
        location_gln=getattr(event, "location_gln", None),
        timestamp=getattr(event, "timestamp"),
        kdes=getattr(event, "kdes", {}),
        input_traceability_lot_codes=getattr(event, "input_traceability_lot_codes", None),
    )


def _rounded_completeness(complete: int, total: int) -> float:
    """Round for the prompt without turning a mixed batch into a boundary value."""
    if complete <= 0:
        return 0.0
    if complete >= total:
        return 1.0
    return min(0.9999, max(0.0001, round(complete / total, 4)))


def _jev_choice(state: dict) -> dict | None:
    """Ask Jev the follow-up Choice. Returns the raw answer dict, or None
    when Jev cannot answer. Fail-open by design."""
    api_key = os.environ.get(_JEV_API_KEY_ENV)
    if not api_key:
        logger.debug("TYPESAFE_API_KEY not set; skipping Jev follow-up")
        return None
    try:
        body = json.dumps(
            {
                "state": state,
                "model": _JEV_MODEL,
                "questions": {
                    "followup": {
                        "type": "choice",
                        "instructions": _FOLLOWUP_INSTRUCTIONS,
                        "criteria": _FOLLOWUP_OPTIONS,
                    }
                }
            }
        ).encode()
        req = urllib.request.Request(
            _JEV_API_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(req, timeout=_JEV_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        answer = data["answers"]["followup"]
        if answer.get("type") != "choice":
            return None
        return answer
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        logger.warning("Jev follow-up failed: %s", type(exc).__name__)
        return None


def summarize_batch(events: list) -> dict:
    """Compute batch statistics in code. All counting here; the model only
    judges. ``events`` are RegEngineEvent-like objects."""
    total = len(events)
    if total == 0:
        return {
            "total_events": 0,
            "frac_with_lot_code": 0.0,
            "frac_kde_complete": 0.0,
            "lineage_links": 0,
            "cte_types": [],
        }
    with_lot = 0
    kde_complete = 0
    lineage_links = 0
    cte_types: set[str] = set()
    for raw_event in events:
        e = _as_regengine_event(raw_event)
        tlc = getattr(e, "traceability_lot_code", "") or ""
        if isinstance(tlc, str) and len(tlc.strip()) >= 3:
            with_lot += 1
        if not any(warning.severity == "required" for warning in validate_event_kdes(e)):
            kde_complete += 1
        cte = getattr(getattr(e, "cte_type", ""), "value", e.cte_type)
        cte_types.add(str(cte))
        inputs = getattr(e, "input_traceability_lot_codes", None) or (
            e.kdes.get("input_traceability_lot_codes")
            if isinstance(getattr(e, "kdes", None), dict)
            else None
        )
        if inputs:
            lineage_links += 1
    return {
        "total_events": total,
        "frac_with_lot_code": round(with_lot / total, 4),
        "frac_kde_complete": _rounded_completeness(kde_complete, total),
        "lineage_links": lineage_links,
        "cte_types": sorted(cte_types),
    }


def _recommend_sync(summary: dict) -> dict | None:
    answer = _jev_choice(summary)
    if answer is None:
        return None
    try:
        choice = answer["choice"]
        raw_confidence = answer.get("confidence", 0.0)
        if not isinstance(choice, str) or isinstance(raw_confidence, bool):
            return None
        if not isinstance(raw_confidence, (int, float, str)):
            return None
        confidence = float(raw_confidence)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (
        choice not in _FOLLOWUP_OPTIONS
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
        or confidence < JEV_MIN_CONFIDENCE
    ):
        return None
    return {
        "action": choice,
        "confidence": round(confidence, 4),
        "options": _FOLLOWUP_OPTIONS,
    }


async def recommend_import_followup(events: list) -> dict | None:
    """Recommend the operator's next action for an imported batch. Returns
    None when Jev is unavailable or unconfident (fail-open). Runs the
    blocking HTTP call in a thread, matching the controller's existing
    asyncio.to_thread convention."""
    summary = summarize_batch(events)
    if summary["total_events"] == 0:
        return None
    return await asyncio.to_thread(_recommend_sync, summary)

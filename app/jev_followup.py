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
import os
import urllib.request

logger = logging.getLogger(__name__)

_JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
_JEV_API_KEY_ENV = "TYPESAFE_API_KEY"
_JEV_MODEL = "jev-latest"
_JEV_TIMEOUT_S = 10.0

# Minimum confidence for a recommendation to be surfaced. Below this the
# judgment is discarded (fail-open). Starting point — validate and adjust.
JEV_MIN_CONFIDENCE = 0.6

# KDE fields required by the inflow-lab RegEngine payload contract
# (see AGENTS.md): presence is checked in code, values never leave.
_REQUIRED_KDE_FIELDS = (
    "cte_type",
    "traceability_lot_code",
    "product_description",
    "quantity",
    "unit_of_measure",
    "location_name",
    "timestamp",
)

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


def _jev_choice(state: dict) -> dict | None:
    """Ask Jev the follow-up Choice. Returns the raw answer dict, or None
    when Jev cannot answer. Fail-open by design."""
    api_key = os.environ.get(_JEV_API_KEY_ENV)
    if not api_key:
        logger.debug("TYPESAFE_API_KEY not set; skipping Jev follow-up")
        return None
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
            },
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
    try:
        with urllib.request.urlopen(req, timeout=_JEV_TIMEOUT_S) as resp:
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
    kde_complete_sum = 0
    lineage_links = 0
    cte_types: set[str] = set()
    for e in events:
        tlc = getattr(e, "traceability_lot_code", "") or ""
        if isinstance(tlc, str) and len(tlc.strip()) >= 3:
            with_lot += 1
        present = 0
        for field in _REQUIRED_KDE_FIELDS:
            v = getattr(e, field, None)
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            present += 1
        kde_complete_sum += present / len(_REQUIRED_KDE_FIELDS)
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
        "frac_kde_complete": round(kde_complete_sum / total, 4),
        "lineage_links": lineage_links,
        "cte_types": sorted(cte_types),
    }


def _recommend_sync(summary: dict) -> dict | None:
    answer = _jev_choice(summary)
    if answer is None:
        return None
    try:
        choice = answer["choice"]
        confidence = float(answer.get("confidence", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    if choice not in _FOLLOWUP_OPTIONS or confidence < JEV_MIN_CONFIDENCE:
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

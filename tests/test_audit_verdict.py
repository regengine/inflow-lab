"""One guard per acceptance criterion of #193.

#193: the audit workbench rendered a green "Signals visible" compliance
verdict whenever `backendAudit()` returned null, because the client-side
placeholder reports `missing: 0` -- literally true (nothing was evaluated,
so nothing is missing) but not the same fact as "the backend scored this
profile and found no gaps". A pending state is not a passing state.

`tests/test_console_behavior.py` covers the console's behaviour broadly;
this file exists to pin #193's four acceptance criteria *individually*, so
a regression in any one of them names the criterion it broke. Two surfaces
the issue cites are only covered here: `pendingAuditModel()`'s explicit
`pending` flag (the issue's own preferred fix), and the lineage timeline's
"not evaluated" card.

Like test_console_behavior.py these run app/static/app.js for real under
node against the DOM stand-in in tests/support/console_dom.js, and assert
on rendered markup rather than on the text of app.js. The harness runner
below is deliberately duplicated rather than imported from another test
module, so this file collects and passes on its own.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs the local node harness, no shell
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "tests" / "support" / "console_dom.js"
RESULT_MARKER = "__RESULT__"

NODE = shutil.which("node") or shutil.which("nodejs")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to run app/static/app.js")


def run_console(snippet: str) -> object:
    """Execute *snippet* inside app.js's own scope and return what it returns."""
    completed = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [str(NODE), str(HARNESS), str(REPO_ROOT)],
        input=snippet,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"console harness failed (exit {completed.returncode}):\n{completed.stderr}"
        )
    stdout = completed.stdout
    assert RESULT_MARKER in stdout, f"harness produced no result:\n{stdout}\n{completed.stderr}"
    return json.loads(stdout.split(RESULT_MARKER, 1)[1])


PRELUDE = """
const SCENARIOS = [
  {
    id: 'leafy_greens_supplier',
    label: 'Leafy greens supplier',
    operation_type: 'supplier',
    industry_type: 'produce',
    reference_format: 'GS1',
    requires_cooling: true,
    description: 'Harvest through packout and shipment.',
  },
  {
    id: 'fresh_cut_processor',
    label: 'Fresh-cut processor',
    operation_type: 'processor',
    industry_type: 'produce',
    reference_format: 'GS1',
    requires_cooling: true,
    description: 'Receiving through transformation and shipment.',
  },
];

function statusFor(scenario, audit) {
  return {
    running: false,
    config: { scenario, delivery: { mode: 'mock', mock_friction: [] } },
    stats: { total_records: 1, unique_lots: 1, delivery: {}, engine: {}, audit },
  };
}

// Shaped like app/audit.py's summarize_scenario_audit() return value.
function auditModel(overrides) {
  return {
    checks: [{ label: 'Cooling recorded', ok: false, detail: 'No cooling CTE seen yet.' }],
    score: 60,
    tone: 'watch',
    label: 'Signals partly visible',
    passed: 3,
    total: 5,
    missing: 2,
    warnings_by_record: {},
    ...overrides,
  };
}

const RECORD = {
  record_id: 'rec-1',
  sequence_no: 1,
  delivery_status: 'posted',
  delivery_attempts: 1,
  destination_mode: 'mock',
  event: {
    cte_type: 'harvesting',
    traceability_lot_code: 'TLC-1',
    product_description: 'Romaine Lettuce',
    location_name: 'Valley Fresh Farms',
    quantity: 10,
    unit_of_measure: 'cases',
    timestamp: '2026-02-10T08:00:00Z',
    kdes: {},
  },
};

const HEALTH = {
  status: 'ok',
  tenant: 'local-demo',
  build: { version: '0.1.0', commit_sha_short: 'abc1234' },
  auth: { enabled: false, uses_default_storage: true },
};

function lineagePayload() {
  return {
    traceability_lot_code: 'TLC-1',
    records: [RECORD],
    nodes: [{
      lot_code: 'TLC-1',
      product_description: 'Romaine Lettuce',
      event_count: 1,
      cte_types: ['harvesting'],
      locations: ['Valley Fresh Farms'],
    }],
    edges: [],
  };
}

// Switch the Line-profile dropdown away from the scenario the backend ran,
// then re-render exactly as the dropdown's own change handler does.
function switchProfileTo(scenario) {
  ids.scenario.value = scenario;
  renderReadinessBanner(activeScenarioSummary(), state.events, state.status);
  renderScenarioWorkbench(state.status, state.events);
  renderEvents(state.events);
}

renderScenarioOptions(SCENARIOS, 'leafy_greens_supplier');
"""


# ---------------------------------------------------------------------------
# Criterion 1 -- "Changing the Line profile to a scenario other than
# status.config.scenario produces a pending/mismatch state, not a passing
# audit verdict."
# ---------------------------------------------------------------------------


def test_criterion_1_profile_mismatch_produces_a_pending_state_not_a_verdict() -> None:
    """The placeholder must carry an explicit `pending` flag (the issue's own
    preferred fix) and the render must branch on it, so a not-computed audit
    can never reach the reassuring branch by way of `missing === 0`."""
    result = run_console(
        PRELUDE
        + """
        renderSnapshot(statusFor('leafy_greens_supplier', auditModel({})), [RECORD], HEALTH);
        switchProfileTo('fresh_cut_processor');
        const placeholder = pendingAuditModel(activeScenarioSummary(), state.status);
        return {
          pending: placeholder.pending,
          mismatched: placeholder.mismatched,
          missing: placeholder.missing,
          total: placeholder.total,
          backendAuditIsNull: backendAudit(state.status, activeScenarioSummary()) === null,
          workbench: ids.scenarioWorkbench.innerHTML,
          alertClass: ids.scenarioWorkbench.querySelector('.scenario-alert').getAttribute('class'),
        };
        """
    )
    # The trigger really is the one #193 describes.
    assert result["backendAuditIsNull"] is True

    # The placeholder still reports missing: 0 -- that is literally true. What
    # it must no longer do is let that value stand in for a passing audit.
    assert result["missing"] == 0
    assert result["total"] == 0
    assert result["pending"] is True, "the placeholder must say it is a placeholder"
    assert result["mismatched"] is True, "and that the mismatch is why"

    assert "is-pending" in result["alertClass"]
    assert "has-warning" not in result["alertClass"]
    assert "Signals visible" not in result["workbench"]
    # It names the profile on screen and the one the backend actually scored.
    assert "Not scored for Fresh-cut processor" in result["workbench"]
    assert "Leafy greens supplier" in result["workbench"]


# ---------------------------------------------------------------------------
# Criterion 2 -- "With a backend audit present and missing > 0, the alert
# still renders `N signal(s) still missing` with the has-warning class."
# ---------------------------------------------------------------------------


def test_criterion_2_a_real_backend_gap_still_renders_as_a_warning() -> None:
    """Suppressing the false pass must not suppress the true failures: a
    scored audit with gaps keeps its warning copy and its warning styling,
    and is never demoted to the pending state."""
    result = run_console(
        PRELUDE
        + """
        renderSnapshot(
          statusFor('leafy_greens_supplier', auditModel({ missing: 2, passed: 3, total: 5 })),
          [RECORD],
          HEALTH,
        );
        return {
          workbench: ids.scenarioWorkbench.innerHTML,
          alertClass: ids.scenarioWorkbench.querySelector('.scenario-alert').getAttribute('class'),
        };
        """
    )
    assert "2 signal(s) still missing" in result["workbench"]
    assert "has-warning" in result["alertClass"]
    assert "is-pending" not in result["alertClass"], "a scored audit is not pending"
    assert "Signals visible" not in result["workbench"]


# ---------------------------------------------------------------------------
# Criterion 3 -- "Shift-log rows render a distinguishable 'not evaluated'
# state when backendAudit() returns null."
# ---------------------------------------------------------------------------


def test_criterion_3_unevaluated_rows_and_lineage_cards_are_distinguishable() -> None:
    """`recordAudit()` used to return a bare [] both when the audit cleared a
    row and when nothing had evaluated it, so an unaudited row rendered
    exactly like a clean one -- in the shift log and in the lineage timeline
    the same helper feeds."""
    result = run_console(
        PRELUDE
        + """
        renderSnapshot(statusFor('leafy_greens_supplier', auditModel({})), [RECORD], HEALTH);
        renderLineage(lineagePayload(), 'TLC-1');
        const scored = {
          rowClass: ids.eventsBody.querySelectorAll('tr')[0].getAttribute('class') || '',
          rowText: ids.eventsBody.innerHTML,
          card: ids.lineageResults.querySelector('.lineage-card').getAttribute('class'),
        };

        switchProfileTo('fresh_cut_processor');
        renderLineage(lineagePayload(), 'TLC-1');
        const unscored = {
          rowClass: ids.eventsBody.querySelectorAll('tr')[0].getAttribute('class') || '',
          rowText: ids.eventsBody.innerHTML,
          card: ids.lineageResults.querySelector('.lineage-card').getAttribute('class'),
          cardText: ids.lineageResults.querySelector('.lineage-card').innerHTML,
          evaluated: recordAudit(RECORD, activeScenarioSummary(), state.status).evaluated,
        };
        return { scored, unscored };
        """
    )
    # A row the backend really did clear stays unmarked -- otherwise the
    # "not evaluated" marker would be noise rather than a distinction.
    assert "audit-not-evaluated" not in result["scored"]["rowClass"]
    assert "audit-not-evaluated" not in result["scored"]["card"]
    assert "not evaluated" not in result["scored"]["rowText"].lower()

    assert result["unscored"]["evaluated"] is False
    assert "audit-not-evaluated" in result["unscored"]["rowClass"]
    assert "Audit not evaluated" in result["unscored"]["rowText"]
    # The lineage timeline renders off the same audit and must agree.
    assert "audit-not-evaluated" in result["unscored"]["card"]
    assert "Audit not evaluated for this line profile." in result["unscored"]["cardText"]


# ---------------------------------------------------------------------------
# Criterion 4 -- "The workbench never renders 'Signals visible' while
# readiness.total === 0."
# ---------------------------------------------------------------------------


def test_criterion_4_signals_visible_never_renders_with_nothing_evaluated() -> None:
    """Both reachable triggers, on both surfaces: a workbench render before
    the first snapshot arrives, and a Line-profile mismatch after one."""
    result = run_console(
        PRELUDE
        + """
        // (1) No status at all yet.
        renderScenarioWorkbench(null, []);
        renderReadinessBanner(activeScenarioSummary(), [], null);
        const beforeFirstSnapshot = {
          total: pendingAuditModel(activeScenarioSummary(), null).total,
          workbench: ids.scenarioWorkbench.innerHTML,
          banner: ids.readinessBanner.innerHTML,
        };

        // (2) A snapshot has landed, then the profile is switched away.
        renderSnapshot(statusFor('leafy_greens_supplier', auditModel({ missing: 0, passed: 5 })), [RECORD], HEALTH);
        const scoredAndClean = ids.scenarioWorkbench.innerHTML;
        switchProfileTo('fresh_cut_processor');
        const afterMismatch = {
          total: pendingAuditModel(activeScenarioSummary(), state.status).total,
          workbench: ids.scenarioWorkbench.innerHTML,
          banner: ids.readinessBanner.innerHTML,
        };
        return { beforeFirstSnapshot, scoredAndClean, afterMismatch };
        """
    )
    for stage in ("beforeFirstSnapshot", "afterMismatch"):
        assert result[stage]["total"] == 0, stage
        assert "Signals visible" not in result[stage]["workbench"], stage
        assert "Signals visible" not in result[stage]["banner"], stage

    # The copy is not simply gone: a genuinely scored, gap-free audit is
    # exactly when "Signals visible" is the true statement to make.
    assert "Signals visible" in result["scoredAndClean"]

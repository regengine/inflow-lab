"""Behavioural tests for the operator console (app/static/app.js).

The console is plain browser JS with no test runner in this repo, so its
defects (#148-#151, #193-#196) had no coverage at all and kept shipping.
These tests run app.js for real under node against the small DOM stand-in
in tests/support/console_dom.js, which parses app/static/index.html so the
markup defaults are the browser's own. Assertions are on observable
behaviour -- rendered markup, form values, focus, request ordering -- not
on the text of app.js, so each one actually fails when its bug is present.
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

# node ships on the GitHub-hosted runners this project's CI uses; the guard
# only covers a stripped-down local environment that has none, so a missing
# interpreter reads as "not exercised here" rather than a spurious failure.
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


# ---------------------------------------------------------------------------
# #150 -- "Clear shift" must clear the lot filters it invalidates
# ---------------------------------------------------------------------------


def test_clear_shift_clears_lot_filters_and_export_links() -> None:
    """resetState() wipes every record, so the lot filters that survive it
    silently point both export links at a lot code that no longer exists."""
    result = run_console(
        """
        __dom.routes({
          '/api/simulate/reset': {},
          ...__dom.snapshotRoutes(),
        });
        ids.exportLot.value = 'TLC-GONE';
        ids.lotLookup.value = 'TLC-GONE';
        updateExportLink();
        const before = ids.exportDownloadLink.href;
        await resetState();
        return {
          before,
          csvHref: ids.exportDownloadLink.href,
          epcisHref: ids.epcisDownloadLink.href,
          lotLookup: ids.lotLookup.value,
          exportLot: ids.exportLot.value,
        };
        """
    )
    assert "traceability_lot_code=TLC-GONE" in result["before"]
    assert "traceability_lot_code" not in result["csvHref"]
    assert "traceability_lot_code" not in result["epcisHref"]
    assert result["lotLookup"] == ""
    assert result["exportLot"] == ""


# ---------------------------------------------------------------------------
# #149 -- a superseded lineage response must not win the panel
# ---------------------------------------------------------------------------

_LINEAGE_SNIPPET = """
const resolvers = {};
function lineagePayload(lot) {
  return {
    traceability_lot_code: lot,
    records: [{
      record_id: `rec-${lot}`,
      sequence_no: 1,
      delivery_status: 'posted',
      delivery_attempts: 1,
      destination_mode: 'mock',
      event: {
        cte_type: 'harvesting',
        traceability_lot_code: lot,
        product_description: `Product ${lot}`,
        location_name: 'Valley Fresh Farms',
        quantity: 10,
        unit_of_measure: 'cases',
        timestamp: '2026-02-10T08:00:00Z',
        kdes: {},
      },
    }],
    nodes: [{
      lot_code: lot,
      product_description: `Product ${lot}`,
      event_count: 1,
      cte_types: ['harvesting'],
      locations: ['Valley Fresh Farms'],
    }],
    edges: [],
  };
}
__dom.setFetch((url) => new Promise((resolve) => {
  const lot = decodeURIComponent(url.split('/').pop());
  resolvers[lot] = () => resolve(__dom.makeResponse({ body: lineagePayload(lot) }));
}));

// Two lot codes clicked in quick succession: LOT-A first, LOT-B second.
ids.lotLookup.value = 'LOT-A';
const first = lookupLineage();
ids.lotLookup.value = 'LOT-B';
const second = lookupLineage();

// The stale one resolves last, which is the whole hazard.
resolvers['LOT-B']();
await second;
resolvers['LOT-A']();
await first;

return {
  lotLookup: ids.lotLookup.value,
  showsB: ids.lineageResults.innerHTML.includes('Product LOT-B'),
  showsA: ids.lineageResults.innerHTML.includes('Product LOT-A'),
  status: ids.statusMessage.textContent,
};
"""


def test_superseded_lineage_response_does_not_overwrite_the_panel() -> None:
    """Clicking two lot codes within a second must leave the panel showing the
    lot the input shows -- not whichever fetch happened to resolve last."""
    result = run_console(_LINEAGE_SNIPPET)
    assert result["lotLookup"] == "LOT-B"
    assert result["showsB"] is True
    assert result["showsA"] is False
    assert "LOT-A" not in result["status"]


# ---------------------------------------------------------------------------
# #151 -- the welcome dialog declares aria-modal, so it must trap focus
# ---------------------------------------------------------------------------

_WELCOME_SNIPPET = """
function activeId() {
  return document.activeElement === document.body ? '<body>' : document.activeElement.id;
}
const page = document.querySelector('main.page');
const opened = {
  overlayVisible: !welcomeEls.overlay.hidden,
  backgroundInert: page.hasAttribute('inert') || page.getAttribute('aria-hidden') === 'true',
  focus: activeId(),
};

// Tab off the last dialog button must wrap to the first, not walk into the
// form hidden behind the backdrop.
welcomeEls.skip.focus();
const forward = await document.dispatchEvent('keydown', { key: 'Tab', shiftKey: false });
const afterForwardWrap = { focus: activeId(), prevented: Boolean(forward.defaultPrevented) };

// Shift+Tab off the first must wrap to the last.
welcomeEls.tour.focus();
const backward = await document.dispatchEvent('keydown', { key: 'Tab', shiftKey: true });
const afterBackwardWrap = { focus: activeId(), prevented: Boolean(backward.defaultPrevented) };

// Focus that is somehow already on a background control is pulled back in.
ids.startBtn.focus();
await document.dispatchEvent('keydown', { key: 'Tab', shiftKey: false });
const afterEscapedFocus = activeId();

// Once dismissed the trap must let go entirely.
hideWelcome();
ids.startBtn.focus();
const released = await document.dispatchEvent('keydown', { key: 'Tab', shiftKey: false });
const afterDismiss = {
  focus: activeId(),
  prevented: Boolean(released.defaultPrevented),
  backgroundInert: page.hasAttribute('inert') || page.getAttribute('aria-hidden') === 'true',
};

return { opened, afterForwardWrap, afterBackwardWrap, afterEscapedFocus, afterDismiss };
"""


def test_welcome_dialog_traps_keyboard_focus() -> None:
    """welcomeOverlay is role=dialog aria-modal=true over a full-viewport
    backdrop; Tab must cycle its own buttons and never reach the page beneath."""
    result = run_console(_WELCOME_SNIPPET)
    assert result["opened"]["overlayVisible"] is True
    assert result["opened"]["focus"] == "welcomeTourBtn"
    assert result["opened"]["backgroundInert"] is True

    assert result["afterForwardWrap"] == {"focus": "welcomeTourBtn", "prevented": True}
    assert result["afterBackwardWrap"] == {"focus": "welcomeSkipBtn", "prevented": True}
    assert result["afterEscapedFocus"] == "welcomeTourBtn"

    # Dismissing the dialog releases both the trap and the background.
    assert result["afterDismiss"]["focus"] == "startBtn"
    assert result["afterDismiss"]["prevented"] is False
    assert result["afterDismiss"]["backgroundInert"] is False


# ---------------------------------------------------------------------------
# #196 -- a 422 must reach the operator as text, not "[object Object]"
# ---------------------------------------------------------------------------

_API_ERROR_SNIPPET = """
const seen = {};

async function capture(name, response) {
  __dom.setFetch(() => response);
  try {
    await api('/api/simulate/start', { method: 'POST', body: '{}' });
    seen[name] = null;
  } catch (error) {
    seen[name] = error.message;
  }
}

// FastAPI's own 422 shape: detail is an ARRAY of error objects.
await capture('fastapiArray', __dom.makeResponse({
  status: 422,
  body: {
    detail: [{
      type: 'value_error',
      loc: ['body', 'config', 'batch_size'],
      msg: 'Value error, batch_size must be between 1 and 100',
      input: 500,
    }],
  },
}));

// Two failures at once must both survive.
await capture('twoErrors', __dom.makeResponse({
  status: 422,
  body: {
    detail: [
      { loc: ['body', 'config', 'interval_seconds'], msg: 'interval_seconds must be >= 0' },
      { loc: ['body', 'config', 'batch_size'], msg: 'batch_size must be between 1 and 100' },
    ],
  },
}));

// The plain-string detail every other endpoint returns is untouched.
await capture('stringDetail', __dom.makeResponse({
  status: 400,
  body: { detail: 'Live delivery requires both api_key and tenant_id' },
}));

// A proxy's HTML 502 has no JSON at all.
await capture('htmlBody', __dom.makeResponse({
  status: 502,
  contentType: 'text/html',
  text: '<html><head><title>502</title></head><body><h1>Bad Gateway</h1></body></html>',
}));

// And a body that is empty entirely.
await capture('emptyBody', __dom.makeResponse({ status: 503, contentType: 'text/plain', text: '' }));

// The operator sees error.message verbatim through setStatus().
setStatus(seen.fastapiArray, 'error', 5000);
seen.statusLine = ids.statusMessage.textContent;
return seen;
"""


def test_api_error_messages_are_readable_for_every_error_body_shape() -> None:
    """`new Error(payload.detail)` stringifies FastAPI's list-shaped 422 detail
    to "[object Object]", discarding the only actionable half of the response."""
    result = run_console(_API_ERROR_SNIPPET)

    assert "[object Object]" not in json.dumps(result)
    assert "config.batch_size" in result["fastapiArray"]
    assert "batch_size must be between 1 and 100" in result["fastapiArray"]
    assert result["statusLine"] == result["fastapiArray"]

    assert "interval_seconds must be >= 0" in result["twoErrors"]
    assert "batch_size must be between 1 and 100" in result["twoErrors"]

    assert result["stringDetail"] == "Live delivery requires both api_key and tenant_id"

    assert "502" in result["htmlBody"]
    assert "Bad Gateway" in result["htmlBody"]
    assert "<" not in result["htmlBody"]

    assert "503" in result["emptyBody"]


def test_the_real_server_422_renders_readably_in_the_console() -> None:
    """End-to-end for #196: take the exact 422 body FastAPI produces for an
    out-of-range Batch size and prove the console turns it into something the
    operator can act on. Pins the wire shape and the rendering together, so a
    change to either side has to keep the operator's status line readable."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/simulate/start", json={"config": {"batch_size": 500}})

    assert response.status_code == 422
    detail = response.json()["detail"]
    # FastAPI's documented HTTPValidationError shape: a list of error objects.
    # That is exactly what `new Error(payload.detail)` used to flatten to
    # "[object Object]".
    assert isinstance(detail, list) and detail, detail

    rendered = run_console(
        f"""
        __dom.setFetch(() => __dom.makeResponse({{
          status: {response.status_code},
          body: {json.dumps(response.json())},
        }}));
        try {{
          await api('/api/simulate/start', {{ method: 'POST', body: '{{}}' }});
          return null;
        }} catch (error) {{
          setStatus(error.message, 'error', 5000);
          return ids.statusMessage.textContent;
        }}
        """
    )
    assert rendered is not None
    assert "[object Object]" not in rendered
    assert "batch_size" in rendered
    assert "batch_size must be between 1 and 100" in rendered


# ---------------------------------------------------------------------------
# #193 -- the workbench must not assert a passing audit it never computed
# ---------------------------------------------------------------------------

_AUDIT_PRELUDE = """
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

renderScenarioOptions(SCENARIOS, 'leafy_greens_supplier');
"""


def test_line_profile_mismatch_does_not_render_a_passing_audit_verdict() -> None:
    """backendAudit() returns null whenever the Line-profile dropdown differs
    from the scenario the backend ran, and the placeholder's `missing: 0` then
    drove the reassuring branch: a green "Signals visible" verdict against
    zero evaluated records."""
    result = run_console(
        _AUDIT_PRELUDE
        + """
        // A real snapshot lands with a real backend audit for leafy greens...
        renderSnapshot(statusFor('leafy_greens_supplier', auditModel({})), [RECORD], HEALTH);
        const beforeSwitch = ids.scenarioWorkbench.innerHTML;

        // ...then the operator switches the Line profile to the processor,
        // which is exactly what guide-rail step 1 invites. state.status still
        // belongs to the previous run, so backendAudit() returns null.
        ids.scenario.value = 'fresh_cut_processor';
        renderReadinessBanner(activeScenarioSummary(), state.events, state.status);
        renderScenarioWorkbench(state.status, state.events);
        renderRecordSpotlight(state.events);
        renderEvents(state.events);
        return {
          beforeSwitch,
          workbench: ids.scenarioWorkbench.innerHTML,
          banner: ids.readinessBanner.innerHTML,
          rowClasses: ids.eventsBody.querySelectorAll('tr').map((row) => row.getAttribute('class') || ''),
          rowText: ids.eventsBody.innerHTML,
        };
        """
    )
    # Before the switch the backend audit really did apply, so the row was
    # rendered as evaluated -- this is the state the switch must not preserve.
    assert "Not scored" not in result["beforeSwitch"]

    workbench = result["workbench"]
    assert "Signals visible" not in workbench
    assert "has-warning" not in workbench
    # It has to say which profile the backend actually scored.
    assert "Fresh-cut processor" in workbench
    assert "Leafy greens supplier" in result["banner"]

    # Shift-log rows must not read as clean when nothing evaluated them.
    assert any("audit-not-evaluated" in cls for cls in result["rowClasses"])
    assert "not evaluated" in result["rowText"].lower()


def test_backend_audit_gaps_still_render_as_warnings() -> None:
    """The reassuring/warning branches must keep working when the audit is
    real -- the fix must not mute genuine gaps."""
    result = run_console(
        _AUDIT_PRELUDE
        + """
        renderSnapshot(statusFor('leafy_greens_supplier', auditModel({ missing: 2 })), [RECORD], HEALTH);
        const gaps = ids.scenarioWorkbench.innerHTML;
        const gapRows = ids.eventsBody.innerHTML;

        renderSnapshot(
          statusFor('leafy_greens_supplier', auditModel({ missing: 0, passed: 5 })),
          [RECORD],
          HEALTH,
        );
        return { gaps, gapRows, clean: ids.scenarioWorkbench.innerHTML };
        """
    )
    assert "2 signal(s) still missing" in result["gaps"]
    assert "has-warning" in result["gaps"]
    assert "Signals visible" not in result["gaps"]
    # An evaluated row is not flagged "not evaluated".
    assert "audit-not-evaluated" not in result["gapRows"]

    assert "Signals visible" in result["clean"]
    assert "has-warning" not in result["clean"]


def test_workbench_before_the_first_snapshot_reports_pending_not_passing() -> None:
    """The other reachable trigger: a workbench render with no status at all."""
    result = run_console(
        _AUDIT_PRELUDE
        + """
        renderScenarioWorkbench(null, []);
        return ids.scenarioWorkbench.innerHTML;
        """
    )
    assert "Signals visible" not in result


# ---------------------------------------------------------------------------
# #194 -- an export preset that needs a lot code must not be offered without one
# ---------------------------------------------------------------------------

_EXPORT_PRELUDE = """
const PRESETS = [
  { id: 'all_records', label: 'All records', description: 'Full FDA-request export.', requires_lot_code: false },
  { id: 'lot_trace', label: 'Lot trace', description: 'Lineage for one lot.', requires_lot_code: true },
];
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
state.events = [RECORD];
renderExportPresetOptions(PRESETS);
"""


def test_lot_trace_preset_without_a_lot_code_is_refused_in_console() -> None:
    """Selecting "Lot trace" with an empty lot field used to open a raw JSON
    400 in a new tab, show nothing in the console, and still mark the guided
    flow's "Export evidence" step done."""
    result = run_console(
        _EXPORT_PRELUDE
        + """
        __dom.setFetch(() => {
          throw new Error('the export must not be requested at all');
        });
        ids.exportPreset.value = 'lot_trace';
        ids.exportLot.value = '';
        updateExportLink();
        const guarded = {
          ariaDisabled: ids.exportDownloadLink.getAttribute('aria-disabled'),
          hint: ids.exportPresetDescription.textContent,
        };
        await ids.exportDownloadLink.dispatchEvent('click');
        return {
          guarded,
          exported: journey.exported,
          status: ids.statusMessage.textContent,
          tone: ids.statusMessage.dataset.tone,
          downloads: __dom.clickLog.length,
        };
        """
    )
    assert result["guarded"]["ariaDisabled"] == "true"
    assert "Traceability Lot Code" in result["guarded"]["hint"]
    assert result["exported"] is False
    assert result["tone"] == "error"
    assert "lot code" in result["status"].lower()
    assert result["downloads"] == 0


def test_failed_export_does_not_mark_the_guided_flow_complete() -> None:
    """A 404 for a typo'd lot code must reach #statusMessage with the backend's
    own detail, and must not advance the guide rail."""
    result = run_console(
        _EXPORT_PRELUDE
        + """
        __dom.setFetch(() => __dom.makeResponse({
          status: 404,
          body: { detail: 'No records found for that lot code' },
        }));
        ids.exportPreset.value = 'all_records';
        ids.exportLot.value = 'TLC-TYPO';
        updateExportLink();
        await ids.exportDownloadLink.dispatchEvent('click');
        return {
          exported: journey.exported,
          status: ids.statusMessage.textContent,
          tone: ids.statusMessage.dataset.tone,
          downloads: __dom.clickLog.length,
        };
        """
    )
    assert result["exported"] is False
    assert result["status"] == "No records found for that lot code"
    assert result["tone"] == "error"
    assert result["downloads"] == 0


def test_successful_export_downloads_a_file_and_marks_the_step_done() -> None:
    """The working path has to keep working: a real file reaches the operator
    and the guide rail advances."""
    result = run_console(
        _EXPORT_PRELUDE
        + """
        __dom.setFetch(() => __dom.makeResponse({
          status: 200,
          contentType: 'text/csv',
          text: 'Traceability Lot Code\\nTLC-1\\n',
        }));
        ids.exportPreset.value = 'lot_trace';
        ids.exportLot.value = 'TLC-1';
        updateExportLink();
        const ariaDisabled = ids.exportDownloadLink.getAttribute('aria-disabled');
        await ids.exportDownloadLink.dispatchEvent('click');
        await ids.epcisDownloadLink.dispatchEvent('click');
        return {
          ariaDisabled,
          exported: journey.exported,
          tone: ids.statusMessage.dataset.tone,
          clicks: __dom.clickLog,
          objectUrls: __dom.objectUrls.map((entry) => entry.revoked),
        };
        """
    )
    assert result["ariaDisabled"] in (None, "false")
    assert result["exported"] is True
    assert result["tone"] != "error"
    downloads = [click for click in result["clicks"] if click["tagName"] == "A" and click["download"]]
    assert len(downloads) == 2, result["clicks"]
    # Object URLs are released rather than leaked for the tab's lifetime.
    assert all(result["objectUrls"])


def test_export_preset_list_still_advertises_the_lot_code_requirement() -> None:
    """The client fix depends on a wire field it previously ignored entirely."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        presets = client.get("/api/mock/regengine/export/presets").json()["presets"]

    by_id = {preset["id"]: preset for preset in presets}
    assert by_id["lot_trace"]["requires_lot_code"] is True
    assert by_id["all_records"]["requires_lot_code"] is False


# ---------------------------------------------------------------------------
# #195 -- a live snapshot must not destroy the rows the operator is using
# ---------------------------------------------------------------------------

_SHIFT_LOG_PRELUDE = """
function record(sequence, overrides = {}) {
  return {
    record_id: `rec-${sequence}`,
    sequence_no: sequence,
    delivery_status: 'posted',
    delivery_attempts: 1,
    destination_mode: 'mock',
    event: {
      cte_type: 'harvesting',
      traceability_lot_code: `TLC-${sequence}`,
      product_description: 'Romaine Lettuce',
      location_name: 'Valley Fresh Farms',
      quantity: 10,
      unit_of_measure: 'cases',
      timestamp: '2026-02-10T08:00:00Z',
      kdes: {},
    },
    ...overrides,
  };
}
function lotButton(lot) {
  return ids.eventsBody.querySelectorAll('[data-lot]').find((node) => node.dataset.lot === lot) || null;
}
function activeLot() {
  return document.activeElement && document.activeElement.dataset
    ? document.activeElement.dataset.lot || '<body>'
    : '<body>';
}
"""


def test_running_line_snapshots_keep_focus_and_row_identity() -> None:
    """The shift log was rebuilt with innerHTML on every snapshot -- about
    every 1.5s while the line runs -- so a focused lot-code button was
    destroyed mid-interaction and focus reverted to <body>."""
    result = run_console(
        _SHIFT_LOG_PRELUDE
        + """
        const first = [record(1), record(2), record(3)];
        renderEvents(first);
        const rowBefore = lotButton('TLC-2').closest('tr');

        lotButton('TLC-2').focus();
        const focusBefore = activeLot();

        // Three more snapshots, each appending a batch, as a running line does.
        renderEvents([...first, record(4)]);
        const afterOne = activeLot();
        renderEvents([...first, record(4), record(5)]);
        renderEvents([...first, record(4), record(5), record(6)]);

        return {
          focusBefore,
          afterOne,
          focusAfterThree: activeLot(),
          // Identity, not markup: a text selection survives a snapshot exactly
          // when the nodes it spans are never replaced.
          sameRowNode: lotButton('TLC-2').closest('tr') === rowBefore,
          rowCount: ids.eventsBody.querySelectorAll('tr').length,
          listeners: ids.eventsBody.listenerCount('click'),
        };
        """
    )
    assert result["focusBefore"] == "TLC-2"
    assert result["afterOne"] == "TLC-2"
    assert result["focusAfterThree"] == "TLC-2"
    assert result["sameRowNode"] is True
    assert result["rowCount"] == 6
    # One delegated listener on #eventsBody, not one per row per tick.
    assert result["listeners"] == 1


def test_changed_row_still_repaints_and_returns_focus() -> None:
    """A row whose delivery outcome changed has to repaint -- and the focused
    lot-code button inside it must come back, not vanish to <body>."""
    result = run_console(
        _SHIFT_LOG_PRELUDE
        + """
        const before = [record(1), record(2)];
        renderEvents(before);
        lotButton('TLC-2').focus();
        const after = [record(1), record(2, { delivery_status: 'failed', error: 'HTTP 429', delivery_attempts: 3 })];
        renderEvents(after);
        return {
          focus: activeLot(),
          markup: ids.eventsBody.innerHTML || ids.eventsBody.querySelectorAll('tr').map((row) => row.innerHTML).join(''),
          status: ids.eventsBody.querySelectorAll('.status-pill').map((pill) => pill.textContent),
        };
        """
    )
    assert result["focus"] == "TLC-2"
    assert "failed" in result["status"]
    assert "HTTP 429" in result["markup"]


def test_delegated_lot_click_still_traces_the_lot() -> None:
    """The delegated listener has to keep the lot buttons working, including
    on rows added by a later snapshot."""
    result = run_console(
        _SHIFT_LOG_PRELUDE
        + """
        const requested = [];
        __dom.setFetch((url) => {
          requested.push(url);
          return __dom.makeResponse({ body: { traceability_lot_code: 'TLC-9', records: [], nodes: [], edges: [] } });
        });
        renderEvents([record(1)]);
        renderEvents([record(1), record(9)]);
        await lotButton('TLC-9').dispatchEvent('click');
        return { lotLookup: ids.lotLookup.value, requested };
        """
    )
    assert result["lotLookup"] == "TLC-9"
    assert result["requested"] == ["/api/lineage/TLC-9"]


def test_overlapping_refreshes_cannot_render_an_older_snapshot() -> None:
    """startFallbackPolling() is a bare 2s setInterval, so a slow response
    could land after a newer one and repaint stale state."""
    result = run_console(
        """
        const pending = [];
        let counter = 0;
        __dom.setFetch((url) => new Promise((resolve) => {
          const total = counter;
          pending.push(() => resolve(__dom.makeResponse({
            body: url.startsWith('/api/events')
              ? { events: [] }
              : url.startsWith('/api/simulate/status')
                ? {
                    running: false,
                    config: { scenario: 'leafy_greens_supplier', delivery: { mode: 'mock', mock_friction: [] } },
                    stats: { total_records: total, unique_lots: total, delivery: {}, engine: {} },
                  }
                : { status: 'ok', tenant: 'local-demo', build: { version: '0.1.0' }, auth: {} },
          })));
        }));

        counter = 1;
        const older = refresh();
        counter = 2;
        const newer = refresh();

        // The newer round-trip finishes first; the older one lands afterwards.
        pending.splice(3).forEach((resolve) => resolve());
        await newer;
        pending.splice(0).forEach((resolve) => resolve());
        await older;

        const rendered = ids.statsGrid.innerHTML;
        const totalCard = /Total records<\\/span>\\s*<strong>(\\d+)<\\/strong>/.exec(rendered);
        return { totalRecords: totalCard ? Number(totalCard[1]) : null };
        """
    )
    assert result["totalRecords"] == 2, "an older refresh repainted over a newer snapshot"


def test_fallback_poll_does_not_stack_overlapping_refreshes() -> None:
    """The poller fires on a fixed interval regardless of whether the previous
    round-trip finished; without a guard those pile up."""
    result = run_console(
        """
        let calls = 0;
        const pending = [];
        __dom.setFetch((url) => new Promise((resolve) => {
          calls += 1;
          pending.push(() => resolve(__dom.makeResponse({
            body: url.startsWith('/api/events')
              ? { events: [] }
              : url.startsWith('/api/simulate/status')
                ? {
                    running: false,
                    config: { scenario: 'leafy_greens_supplier', delivery: { mode: 'mock', mock_friction: [] } },
                    stats: { delivery: {}, engine: {} },
                  }
                : { status: 'ok', build: {}, auth: {} },
          })));
        }));

        const firstTick = pollForFreshness();
        const duringFlight = calls;
        const secondTick = pollForFreshness();
        const afterSecondTick = calls;
        pending.splice(0).forEach((resolve) => resolve());
        await Promise.all([firstTick, secondTick]);
        return { duringFlight, afterSecondTick };
        """
    )
    assert result["duringFlight"] == 3, "one poll should issue exactly the three refresh() requests"
    assert result["afterSecondTick"] == 3, "a second poll fired while one was in flight"


# ---------------------------------------------------------------------------
# #148 -- a stale form must not downgrade or misdirect live delivery
#
# Two halves, because the defect has two: the console's form did not reflect
# the server's real delivery config after a reload, and the server treated an
# omitted credential in a request body as "clear it".
# ---------------------------------------------------------------------------

_LIVE_ENDPOINT = "https://staging.regengine.example/api/v1/webhooks/ingest"
_LIVE_TENANT = "11111111-1111-1111-1111-111111111111"
_LIVE_KEY = "rge_live_reload_secret"


def test_reloaded_console_form_agrees_with_the_servers_delivery_config() -> None:
    """The header badges always read "Connected" from server status, but the
    Delivery dropdown fell back to its raw HTML default of Sandbox -- and the
    form is what buildConfig() sends on the next Start/Clear shift/Retry."""
    result = run_console(
        f"""
        const routes = __dom.snapshotRoutes();
        routes['/api/simulate/status'] = {{
          running: false,
          config: {{
            source: 'codex-simulator',
            scenario: 'leafy_greens_supplier',
            scale: 'midsize',
            interval_seconds: 1.5,
            batch_size: 3,
            seed: null,
            persist_path: 'data/events.jsonl',
            delivery: {{
              mode: 'live',
              endpoint: {json.dumps(_LIVE_ENDPOINT)},
              api_key: null,
              tenant_id: null,
              mock_friction: [],
            }},
          }},
          stats: {{ total_records: 0, unique_lots: 0, delivery: {{}}, engine: {{}}, audit: null }},
        }};
        __dom.routes(routes);

        const onLoad = {{ mode: ids.deliveryMode.value, endpoint: ids.endpoint.value }};
        await refresh();
        const afterReload = {{
          mode: ids.deliveryMode.value,
          endpoint: ids.endpoint.value,
          headerPill: ids.deliveryModePill.textContent,
          connectionPill: ids.connectionPill.textContent,
          submitted: buildConfig().delivery,
        }};

        // An operator mid-edit must win over the next snapshot tick.
        ids.deliveryMode.value = 'none';
        await ids.deliveryMode.dispatchEvent('change');
        ids.apiKey.value = 'typed-but-not-submitted';
        await ids.apiKey.dispatchEvent('input');
        await refresh();
        const afterEdit = {{ mode: ids.deliveryMode.value, apiKey: ids.apiKey.value }};

        return {{ onLoad, afterReload, afterEdit }};
        """
    )
    # The raw markup default, before any hydration -- this is what used to win.
    assert result["onLoad"] == {"mode": "mock", "endpoint": ""}

    assert result["afterReload"]["mode"] == "live"
    assert result["afterReload"]["endpoint"] == _LIVE_ENDPOINT
    assert result["afterReload"]["headerPill"] == "connected"
    assert result["afterReload"]["connectionPill"] == "Connected"
    # The form is what gets submitted, so this is the criterion that matters.
    assert result["afterReload"]["submitted"]["mode"] == "live"
    assert result["afterReload"]["submitted"]["endpoint"] == _LIVE_ENDPOINT
    # api_key is never returned by status(); the client must send null, not "".
    assert result["afterReload"]["submitted"]["api_key"] is None

    assert result["afterEdit"] == {"mode": "none", "apiKey": "typed-but-not-submitted"}


def _connect_live_workspace(client) -> None:
    response = client.post(
        "/api/integration/configure",
        json={
            "mode": "live",
            "endpoint": _LIVE_ENDPOINT,
            "api_key": _LIVE_KEY,
            "tenant_id": _LIVE_TENANT,
        },
    )
    assert response.status_code == 200, response.text


def _post_reload_delivery_block() -> dict:
    """Exactly what buildConfig() sends after a reload: mode and endpoint
    hydrated from status, credentials null because status never returns them."""
    return {
        "mode": "live",
        "endpoint": _LIVE_ENDPOINT,
        "api_key": None,
        "tenant_id": None,
        "mock_friction": [],
    }


def test_clear_shift_after_reload_keeps_the_live_credentials() -> None:
    """reset() replaced the whole stored config with the submitted one, so the
    console's post-reload body -- which cannot carry the API key -- wiped it."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app, controller
    from app.schemas.simulation import SimulationConfig

    asyncio.run(controller.reset(SimulationConfig()))
    try:
        with TestClient(app) as client:
            _connect_live_workspace(client)

            status = client.get("/api/simulate/status").json()
            assert status["config"]["delivery"]["api_key"] is None, "status must keep masking the key"

            response = client.post(
                "/api/simulate/reset",
                json={
                    "source": "codex-simulator",
                    "scenario": "leafy_greens_supplier",
                    "scale": "midsize",
                    "interval_seconds": 1.5,
                    "batch_size": 3,
                    "seed": None,
                    "persist_path": "data/events.jsonl",
                    "delivery": _post_reload_delivery_block(),
                },
            )
            assert response.status_code == 200, response.text

            integration = client.get("/api/integration/status").json()
            assert integration["mode"] == "live"
            assert integration["api_key_configured"] is True
            assert integration["tenant_configured"] is True
    finally:
        asyncio.run(controller.reset(SimulationConfig()))


def test_retry_after_reload_posts_to_the_live_endpoint_not_the_sandbox() -> None:
    """The console promises "Retry failed is always safe". A retry built from a
    post-reload form must still reach RegEngine with the stored credentials --
    not be quietly absorbed by the built-in mock and reported as posted."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app, controller
    from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
    from app.schemas.simulation import SimulationConfig

    seen: list[dict] = []

    class _RecordingLiveResult:
        response = {"accepted": 1, "rejected": 0, "events": []}
        metadata = {"delivery_mode": "live"}

    async def _recording_ingest(payload, config, idempotency_key=None):
        seen.append(
            {
                "endpoint": str(config.delivery.endpoint),
                "api_key": config.delivery.api_key,
                "tenant_id": config.delivery.tenant_id,
            }
        )
        return _RecordingLiveResult()

    asyncio.run(controller.reset(SimulationConfig()))
    original_ingest = controller.live_client.ingest
    controller.live_client.ingest = _recording_ingest
    try:
        with TestClient(app) as client:
            _connect_live_workspace(client)
            controller.store.add_many(
                [
                    StoredEventRecord(
                        payload_source="console-reload-suite",
                        event=RegEngineEvent(
                            cte_type=CTEType.HARVESTING,
                            traceability_lot_code="TLC-RELOAD-0001",
                            product_description="Romaine Lettuce",
                            quantity=100,
                            unit_of_measure="cases",
                            location_name="Valley Fresh Farms",
                            timestamp="2026-03-01T08:00:00Z",
                            kdes={
                                "harvest_date": "2026-03-01",
                                "reference_document": "Harvest Log HL-RELOAD",
                            },
                        ),
                        destination_mode=DestinationMode.LIVE,
                        delivery_status="failed",
                        error="temporary outage",
                    )
                ]
            )

            response = client.post(
                "/api/delivery/retry",
                json={"delivery": _post_reload_delivery_block(), "source": "codex-simulator"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["delivery_mode"] == "live"
    finally:
        controller.live_client.ingest = original_ingest
        asyncio.run(controller.reset(SimulationConfig()))

    assert seen, "the retry never reached the live client -- it went to the sandbox"
    assert seen[0]["endpoint"] == _LIVE_ENDPOINT
    assert seen[0]["api_key"] == _LIVE_KEY
    assert seen[0]["tenant_id"] == _LIVE_TENANT


def test_credentials_are_never_carried_across_to_a_different_endpoint() -> None:
    """"Omitted means unchanged" must not become "inherit this key for
    whatever host you just typed in"."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app, controller
    from app.schemas.simulation import SimulationConfig

    asyncio.run(controller.reset(SimulationConfig()))
    try:
        with TestClient(app) as client:
            _connect_live_workspace(client)

            response = client.post(
                "/api/simulate/reset",
                json={
                    "delivery": {
                        "mode": "mock",
                        "endpoint": "https://someone-elses.regengine.example/api/v1/webhooks/ingest",
                        "api_key": None,
                        "tenant_id": None,
                        "mock_friction": [],
                    },
                },
            )
            assert response.status_code == 200, response.text

            integration = client.get("/api/integration/status").json()
            assert integration["endpoint_host"] == "someone-elses.regengine.example"
            assert integration["api_key_configured"] is False
            assert integration["tenant_configured"] is False
    finally:
        asyncio.run(controller.reset(SimulationConfig()))


def test_a_directly_submitted_credential_still_replaces_the_stored_one() -> None:
    """The merge only fills gaps; an operator who types a new key must get it."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app, controller
    from app.schemas.simulation import SimulationConfig

    asyncio.run(controller.reset(SimulationConfig()))
    try:
        with TestClient(app) as client:
            _connect_live_workspace(client)
            response = client.post(
                "/api/simulate/reset",
                json={
                    "delivery": {
                        "mode": "live",
                        "endpoint": _LIVE_ENDPOINT,
                        "api_key": "rge_live_rotated_key",
                        "tenant_id": "22222222-2222-2222-2222-222222222222",
                        "mock_friction": [],
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert controller.config.delivery.api_key == "rge_live_rotated_key"
            assert controller.config.delivery.tenant_id == "22222222-2222-2222-2222-222222222222"
    finally:
        asyncio.run(controller.reset(SimulationConfig()))


def test_a_direct_controller_reset_still_clears_everything() -> None:
    """The merge lives at the HTTP boundary on purpose: controller.reset(
    SimulationConfig()) is how every test file returns to a clean default, and
    it must keep clearing credentials outright."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app, controller
    from app.schemas.simulation import SimulationConfig

    asyncio.run(controller.reset(SimulationConfig()))
    with TestClient(app) as client:
        _connect_live_workspace(client)
        assert controller.config.delivery.api_key == _LIVE_KEY

    asyncio.run(controller.reset(SimulationConfig()))
    assert controller.config.delivery.api_key is None
    assert controller.config.delivery.tenant_id is None
    assert controller.config.delivery.mode.value == "mock"

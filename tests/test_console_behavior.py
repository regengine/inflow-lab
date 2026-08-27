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

"""Console (app/static) behaviour that can be pinned from the server side.

The operator console is dependency-free vanilla JS with no JS test runner in
this repo, so the accessibility and contract-facing properties of the fixes in
issues #148-#153, #176-#183 and #193-#196 are asserted here two ways:

* against the shipped static assets, for markup/CSS invariants (live regions,
  accessible names, ARIA state, WCAG contrast of the tokens the console
  actually uses); and
* against the API, for the response shapes the console now has to render
  (FastAPI's list-shaped 422 ``detail``, the ``requires_lot_code`` preset
  flag, and the 400 a lot-requiring preset raises without a lot code).

Contrast ratios are computed with the WCAG 2.1 relative-luminance formula
rather than hard-coded, so re-tuning a token is caught rather than assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
# The console ships as several ES modules (see issue #154). These assertions pin
# console *behavior*, not file layout, so read every script as one corpus —
# otherwise splitting app.js would silently stop enforcing them.
APP_JS = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(STATIC.glob("*.js"))
)
STYLES_CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def _relative_luminance(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    light, dark = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _css_token(name: str) -> str:
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", STYLES_CSS)
    assert match, f"{name} is not defined in styles.css"
    return match.group(1)


def test_contrast_helper_matches_known_wcag_pairs():
    assert round(_contrast("#ffffff", "#000000"), 2) == 21.0
    # The pre-fix accent (#059669 on white text) is the ratio issue #177 cites.
    assert round(_contrast("#059669", "#ffffff"), 2) == 3.77


def test_primary_button_text_clears_wcag_aa():
    """#177: white .button text on the accent background needs >= 4.5:1."""
    button_rule = re.search(r"\.button \{(.*?)\}", STYLES_CSS, re.S)
    assert button_rule, ".button rule missing"
    assert "var(--accent-button)" in button_rule.group(1)
    assert _contrast(_css_token("--accent-button"), "#ffffff") >= 4.5


def test_focus_ring_clears_non_text_contrast_on_light_and_dark():
    """#176: the focus indicator needs >= 3:1 on every surface it lands on."""
    focus_ring = _css_token("--focus-ring")
    assert "rgba(0, 127, 115, 0.25)" not in STYLES_CSS
    assert _contrast(focus_ring, _css_token("--surface")) >= 3.0
    assert _contrast(focus_ring, _css_token("--surface-strong")) >= 3.0
    assert "outline: 3px solid var(--focus-ring)" in STYLES_CSS


def test_result_panels_are_live_regions():
    """#181: action outcomes must be announced without moving focus."""
    for element_id in ("statusMessage", "connectionResult", "deliverySummary", "importResults"):
        match = re.search(rf"<[^>]*id=\"{element_id}\"[^>]*>", INDEX_HTML)
        assert match, f"#{element_id} not found in index.html"
        tag = match.group(0)
        assert 'role="status"' in tag, tag
        assert 'aria-live="polite"' in tag, tag
    # Errors interrupt rather than queue behind other speech.
    assert "aria-live', tone === 'error' ? 'assertive' : 'polite'" in APP_JS


def test_quick_trace_input_has_an_accessible_name():
    """#179: the guided tour focuses #lotLookup, so it needs a real label."""
    assert '<label class="sr-only" for="lotLookup">' in INDEX_HTML
    assert re.search(r"\.sr-only \{", STYLES_CSS)


def test_guide_steps_expose_role_and_current_state():
    """#183: the stepper's interactivity and progress were CSS-only."""
    steps = re.findall(r"<li data-guide-step=\"[^\"]+\"[^>]*>", INDEX_HTML)
    assert len(steps) == 5
    for step in steps:
        assert 'role="button"' in step, step
        assert 'tabindex="0"' in step, step
    assert "setAttribute('aria-current', 'step')" in APP_JS
    assert "removeAttribute('aria-current')" in APP_JS


def test_welcome_dialog_traps_tab():
    """#151: role=dialog aria-modal=true has to behave like a modal."""
    assert "function trapWelcomeFocus(event)" in APP_JS
    assert "document.addEventListener('keydown', trapWelcomeFocus, true)" in APP_JS
    assert "document.removeEventListener('keydown', trapWelcomeFocus, true)" in APP_JS


def test_no_unescaped_interpolation_inside_html_attributes():
    """#153: every ${...} inside a quoted attribute goes through escapeHtml()."""
    offenders = [
        match.group(0)
        for match in re.finditer(r"=\"\$\{(?!escapeHtml\()[^}]*\}\"", APP_JS)
    ]
    assert offenders == [], offenders
    assert 'data-tone="${escapeHtml(readiness.tone)}"' in APP_JS


def test_clear_shift_also_clears_the_lot_filters():
    """#150: stale lot filters made the next export silently return nothing."""
    reset = re.search(r"async function resetState\(\) \{(.*?)\n\}", APP_JS, re.S)
    assert reset, "resetState() not found"
    body = reset.group(1)
    assert "ids.lotLookup.value = ''" in body
    assert "ids.exportLot.value = ''" in body
    assert "updateExportLink()" in body


def test_lineage_lookups_drop_superseded_responses():
    """#149: the last click must win, not the last response."""
    lookup = re.search(r"async function lookupLineage\(\) \{(.*?)\n\}", APP_JS, re.S)
    assert lookup, "lookupLineage() not found"
    body = lookup.group(1)
    assert "const requestSeq = ++lineageRequestSeq;" in body
    assert body.count("if (requestSeq !== lineageRequestSeq)") == 2


def test_refresh_failures_are_reported_not_unhandled():
    """#152: the Refresh button's failure path had no reporting at all."""
    refresh = re.search(r"async function refresh\(\) \{(.*?)\n\}", APP_JS, re.S)
    assert refresh, "refresh() not found"
    assert "setStatus(error.message, 'error', 5000)" in refresh.group(1)
    assert "runWithBusy(button, handler).catch(" in APP_JS


def test_shift_log_patches_rows_instead_of_rebuilding():
    """#195: an innerHTML rebuild per snapshot destroyed focus and selection."""
    render = re.search(r"function renderEvents\(events\) \{(.*?)\n\}\n", APP_JS, re.S)
    assert render, "renderEvents() not found"
    body = render.group(1)
    # The empty-state placeholder is the only innerHTML assignment left.
    assert body.count("body.innerHTML =") == 1
    assert "state.eventRows" in body
    assert "record.record_id" in body
    # One delegated listener for the table, not one per row per tick.
    assert "ids.eventsBody.addEventListener('click'" in APP_JS
    assert "querySelectorAll('[data-lot]').forEach" not in APP_JS


def test_snapshot_renders_are_ordered():
    """#195 (4): a slow response must not repaint over a newer one."""
    assert "const requestSeq = ++snapshotSeq;" in APP_JS
    assert "if (requestSeq !== snapshotSeq)" in APP_JS
    assert "if (state.refreshInFlight)" in APP_JS
    assert "revision < Number(state.lastRevision || 0)" in APP_JS


def test_delivery_form_hydrates_from_server_state():
    """#148: a reload left the form on Sandbox while the server ran live."""
    assert "function hydrateDeliveryForm(status = state.status)" in APP_JS
    assert "hydrateField('deliveryMode', delivery.mode || 'mock')" in APP_JS
    assert "hydrateDeliveryForm(status);" in APP_JS
    # A blank endpoint no longer silently means the production URL in sandbox.
    assert (
        "ids.endpoint.value.trim() || (ids.deliveryMode.value === 'live' ? DEFAULT_LIVE_INGEST_ENDPOINT : '')"
        in APP_JS
    )
    # Every config-replacing action refuses to post credentials it never had.
    assert APP_JS.count("if (blockedByMissingCredentials()) {") == 5


def test_pending_audit_never_renders_a_passing_verdict():
    """#193: the placeholder's missing=0 drove the reassuring branch."""
    assert "pending: true" in APP_JS
    assert "const auditPending = Boolean(readiness.pending) || !readiness.total;" in APP_JS
    verdict = re.search(r"const verdict = auditPending(.*?);\n", APP_JS, re.S)
    assert verdict, "verdict branch not found"
    assert "'Signals visible'" in verdict.group(1)
    # 'Signals visible' must exist only inside the guarded branch.
    assert APP_JS.count("'Signals visible'") == 1
    # Rows say "not evaluated" instead of rendering as clean.
    assert "return { evaluated: false, messages: [] };" in APP_JS
    assert "Audit not evaluated" in APP_JS


def test_export_presets_advertise_their_lot_code_requirement():
    """#194: the console now reads the flag the backend already ships."""
    response = client.get("/api/mock/regengine/export/presets")
    assert response.status_code == 200
    presets = {preset["id"]: preset for preset in response.json()["presets"]}
    assert presets["lot_trace"]["requires_lot_code"] is True
    assert presets["all_records"]["requires_lot_code"] is False
    assert "requires_lot_code" in APP_JS
    assert "function exportBlockedReason()" in APP_JS


def test_lot_requiring_export_without_a_lot_code_is_a_client_visible_error():
    """#194: this is the response that used to open as a raw JSON page."""
    response = client.get("/api/mock/regengine/export/fda-request", params={"preset": "lot_trace"})
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    # Exports go through fetch() now, so the detail reaches #statusMessage and
    # the guided step is only marked done after a confirmed 2xx.
    assert "async function downloadExport(link, label)" in APP_JS
    assert "throw new Error(await errorMessageFor(response));" in APP_JS
    exporter = re.search(r"async function downloadExport\(link, label\) \{(.*?)\n\}\n", APP_JS, re.S)
    assert exporter and "journey.exported = true;" in exporter.group(1)
    assert APP_JS.count("journey.exported = true;") == 1


def test_validation_failures_render_as_field_level_messages():
    """#196: FastAPI's 422 detail is a list of objects, not a string."""
    payload = {
        "config": {
            "source": "console-test",
            "scenario": "leafy_greens_supplier",
            "batch_size": 500,
            "delivery": {"mode": "mock"},
        }
    }
    response = client.post("/api/simulate/start", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert "batch_size" in detail[0]["loc"]
    assert "between 1 and 100" in detail[0]["msg"]
    # The console flattens exactly this shape rather than stringifying it.
    assert "function formatErrorDetail(detail)" in APP_JS
    assert "async function errorMessageFor(response)" in APP_JS
    assert "payload.detail ||" not in APP_JS

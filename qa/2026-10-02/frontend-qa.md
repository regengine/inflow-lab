# Frontend / Interface QA — regengine/inflow-lab @ main (static, read-only)

**Date:** 2026-10-02 · **Scope:** `app/static/` (operator console: `index.html` 454 lines, `app.js` 1878 lines, `styles.css`) plus the docs/interface surface. Static review only — no browser run, no JS execution.
**Environment note:** the sandbox VM restarted mid-review; evidence was re-fetched into the persistent `evidence/` directory. Nothing was modified.

## What was inspected

- `app/static/index.html` — full structure, form controls, a11y markers, static copy
- `app/static/app.js` — state handling, `api()` wrapper, error/empty/loading states, HTML escaping, destructive-action patterns, onboarding/tour
- `app/static/styles.css` — spot-checked only (not line-reviewed)
- Docs/interface surface: README "API reference" vs routers (done in backend pass — all match), `DESIGN_PARTNER_DEMO_SCRIPT.md` UI labels vs actual UI labels, internal markdown link check across all docs

## Console structure

Vanilla JS, no build step, no frontend framework — matches the repo's stated "keep dependencies light" convention (`AGENTS.md`). `package.json` exists only for the eslint lint gate; no production JS dependencies. Panels: hero/welcome, guide rail (5-step tour, persisted in localStorage), shift summary strip, line setup, line control (start/pause/step/clear), delivery monitor, lot lineage, audit-readiness lens, CSV import, evidence exports (FDA CSV / EPCIS JSON-LD), RegEngine integration settings with failure-rehearsal toggles.

## Strengths (verified in code)

- **Accessibility markers present throughout:** `aria-label` on sections and the lot lookup input, `role="button"` + `tabindex="0"` on guide-rail steps, `role="status"` + `aria-live="polite"` on `#statusMessage` (upgraded to `assertive` for errors — `app.js:364`), `#connectionResult`, `#deliverySummary`. Live-region text is only touched when it changes (`app.js:360-361`).
- **Output escaping consistent:** every dynamic value interpolated into `innerHTML` templates goes through `escapeHtml` (event table rows `app.js:1110-1132`, lineage `1144`/`1244`, error messages `1510`/`1741`). No unescaped interpolation found in the reviewed templates.
- **Destructive action is two-click armed:** "Clear shift" requires a second click within a 6s arm window (`app.js:1760-1784`), with an explanatory status message. No `confirm()` dialogs anywhere — the arming pattern is used instead.
- **Error states surfaced:** failed fetches throw `Error(payload.detail || 'Request failed: <status>')` (`app.js:397-398`) and are routed to `setStatus(..., 'error')` with 4–6s hold across ~15 call sites; empty states are explicit ("No events yet. Load a fixture or run a single batch." `app.js:1099`; "No saved scenarios"; "Readiness scoring will appear once scenario metadata loads.").
- **Secret hygiene in UI:** API key field is `type="password"` + `autocomplete="off"` (`index.html:218`); connection status renders only "Configured"/"Not set" (`app.js:1418`); the tenant-id field has `autocomplete="off"`.
- **Form Enter-key trap:** implicit form submission is suppressed so Enter doesn't reload the page (`app.js` after the reset handler).
- **Loading indicators:** "Testing connection…" shown during the connection probe (`app.js:1477`); buttons get a busy state via `runWithBusy`.

## Findings

### LOW
1. **SSE stream has no visible connecting/reconnecting state** — the dashboard subscribes to `/api/simulate/stream` with a refresh-polling fallback (per README), but the reviewed JS shows no loading/reconnecting indicator for the stream itself; a silently dropped stream degrades to stale data until the next poll without telling the operator. (Not fully traced through the stream handler — flag for a live check.)
2. **`Refresh`-triggered reloads have no pending indicator** — several `refresh()` calls (`app.js:1874` and others) can leave the events/stats panels visibly stale during slow responses with no spinner or "updating" state.

### Notes (no severity)
- Copy is consistent between docs and UI: the demo script's "Sandbox (built-in stand-in)" label matches `index.html:207` exactly; scenario/fixture names and lot codes (`TLC-DEMO-FC-OUT-001`, `BATCH-DEMO-FC-001`) verified present in `app/demo_fixtures.py`; the fresh-cut fixture contains exactly 13 events as the demo script claims.
- `styles.css` was not line-reviewed (out of scope for the checklist; structure-only spot check).
- All internal markdown links in docs resolve (LICENSE/NOTICE exist in the repo tree).
- The browser cannot be exercised here, so first-run welcome dialog, guided tour, and SSE fallback behavior are documented-only claims — carried as UX walkthrough notes, not verified.

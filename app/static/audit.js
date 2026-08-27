// Audit readiness: which scenario is selected, what the backend audit says
// about it, and the two panels that render that verdict. The pending model
// lives here so no surface can render "no gaps found" for an unscored run.

import { ids, state } from './dom.js';
import { escapeHtml } from './format.js';
import { operationTypeLabel, scenarioLabel } from './labels.js';

export function activeScenarioSummary() {
  return state.scenarioCatalog[ids.scenario.value] || null;
}

export function backendAudit(status = state.status, summary = activeScenarioSummary()) {
  const audit = status?.stats?.audit || null;
  const statusScenario = status?.config?.scenario;
  if (!audit || !summary || !statusScenario || statusScenario !== summary.id) {
    return null;
  }
  return audit;
}

export function sourceCteForScenario(summary) {
  if (summary?.industry_type === 'seafood') {
    return 'First land-based receiving';
  }
  return 'Harvesting';
}

export function scenarioNarrative(summary) {
  if (!summary) {
    return 'Choose a scenario to see which audit signals and reference rules the lab should surface.';
  }
  if (summary.industry_type === 'seafood') {
    return 'This flow should prove vessel-linked first receipt, dockside handoff, and GS1-linked shipping continuity.';
  }
  if (summary.industry_type === 'dairy') {
    return 'This flow should prove continuous movement through silos and vats without forcing produce-style cooling records.';
  }
  return 'This flow should prove field-level origin, packout packaging changes, and downstream traceability through transformation and shipment.';
}

// Placeholder used until the backend audit for the *selected* scenario is
// available. `pending` is the flag every verdict surface branches on: its
// missing count is 0 because nothing was evaluated, which must never be
// rendered as "no gaps found".
export function pendingAuditModel(summary, status = state.status) {
  const ranScenario = status?.config?.scenario || null;
  const mismatch = Boolean(summary && ranScenario && ranScenario !== summary.id);
  return {
    pending: true,
    mismatch,
    checks: [],
    score: 0,
    tone: 'watch',
    label: mismatch ? 'Not scored for this line profile' : 'Awaiting simulator audit',
    passed: 0,
    total: 0,
    missing: 0,
    detail: mismatch
      ? `Showing ${summary.label}, but the line last ran ${scenarioLabel(ranScenario)} — start the line to score this profile.`
      : summary
        ? `${summary.label} needs a simulator status refresh before audit scoring can be shown.`
        : 'Run the simulator to load backend audit scoring.',
  };
}

export function renderReadinessBanner(summary, events, status = state.status) {
  if (!summary) {
    ids.readinessBanner.innerHTML = '<p class="note">Readiness scoring will appear once scenario metadata loads.</p>';
    return;
  }
  const readiness = backendAudit(status, summary) || pendingAuditModel(summary, status);
  // Required gaps would fail live ingest; recommended ones would not, so they
  // are counted separately and the banner never blends a blocker into a
  // nice-to-have. A pending model scored nothing, so it may not claim zero.
  const scored = !readiness.pending && Boolean(readiness.total);
  const requiredWarnings = Number(readiness.required_warning_count) || 0;
  const recommendedWarnings = Number(readiness.recommended_warning_count) || 0;
  const severityClass = !scored
    ? 'readiness-advisory'
    : requiredWarnings
      ? 'readiness-blocker'
      : 'readiness-advisory';
  const severityLabel = !scored
    ? 'Required vs recommended pending'
    : requiredWarnings
      ? `${requiredWarnings} required KDE gap(s), ${recommendedWarnings} recommended`
      : `0 required KDE gaps, ${recommendedWarnings} recommended`;
  ids.readinessBanner.innerHTML = `
    <div class="readiness-banner-shell" data-tone="${escapeHtml(readiness.tone)}">
      <div class="readiness-score">
        <span>Readiness</span>
        <strong>${escapeHtml(readiness.score)}</strong>
        <small>/100</small>
      </div>
      <div class="readiness-copy">
        <h3>${escapeHtml(readiness.label)}</h3>
        <p>${escapeHtml(readiness.detail || `${summary.label} is currently showing ${readiness.passed} of ${readiness.total} expected audit signals.`)}</p>
      </div>
      <div class="readiness-meta">
        <span>${escapeHtml(summary.reference_format)} references</span>
        <span>${escapeHtml(summary.requires_cooling ? 'Cooling required' : 'Continuous or direct flow')}</span>
        <span>${escapeHtml(readiness.total ? `${readiness.missing} gap(s) still visible` : 'Backend audit pending')}</span>
        <span class="${escapeHtml(severityClass)}">${escapeHtml(severityLabel)}</span>
      </div>
    </div>
  `;
}

// A warning whose severity the backend did not send is treated as advisory —
// only an explicit "required" may be rendered as a live-ingest blocker.
export function warningSeverity(warning) {
  return warning?.severity === 'required' ? 'required' : 'recommended';
}

// Returns {evaluated, warnings, messages, requiredCount}. `warnings` carries
// the severity for each entry, required-first; `messages` is the flat text
// list. When the backend audit does not cover the selected scenario nothing
// was evaluated, and a row must say so rather than render as clean.
export function recordWarnings(record, summary, status = state.status) {
  const audit = backendAudit(status, summary);
  if (!audit) {
    // Nothing was evaluated, so there is nothing to grade: callers read the
    // optional `warnings`/`requiredCount` defensively for exactly this case.
    return { evaluated: false, messages: [] };
  }
  const warningPayload = audit.warnings_by_record?.[record.record_id];
  if (Array.isArray(warningPayload) && warningPayload.length) {
    const warnings = warningPayload
      .filter((warning) => typeof warning?.message === 'string' && warning.message)
      .map((warning) => ({ message: warning.message, severity: warningSeverity(warning) }))
      // The backend already orders required first; re-sorting here keeps that
      // true even if a payload ever arrives in another order.
      .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'required' ? -1 : 1));
    return {
      evaluated: true,
      warnings,
      messages: warnings.map((warning) => warning.message),
      requiredCount: warnings.filter((warning) => warning.severity === 'required').length,
    };
  }
  return { evaluated: true, warnings: [], messages: [], requiredCount: 0 };
}

export function renderScenarioWorkbench(status = state.status, events = state.events) {
  const summary = activeScenarioSummary();
  if (!summary) {
    ids.scenarioWorkbench.innerHTML = '<p class="note">Scenario metadata will appear here once presets load.</p>';
    return;
  }
  const readiness = backendAudit(status, summary) || pendingAuditModel(summary, status);
  const checks = readiness.checks;
  const sourceCte = sourceCteForScenario(summary);
  const eventCount = (events || []).length;
  const warningCount = readiness.missing;
  // "Signals visible" is a passing compliance verdict, so it may only be
  // rendered when the backend actually scored this scenario. A pending or
  // scenario-mismatched model reports missing = 0 because nothing was
  // evaluated — not because nothing is missing.
  const auditPending = Boolean(readiness.pending) || !readiness.total;
  const verdict = auditPending
    ? readiness.mismatch
      ? 'Not scored for this line profile'
      : 'Audit pending'
    : warningCount
      ? `${warningCount} signal(s) still missing`
      : 'Signals visible';
  const transformCount = (events || []).filter((record) => record.event.cte_type === 'transformation').length;
  const cards = [
    ['Operation', operationTypeLabel(summary.operation_type)],
    ['Industry', summary.industry_type],
    ['Reference format', summary.reference_format],
    ['Source CTE', sourceCte],
    ['Cooling model', summary.requires_cooling ? 'Required' : 'Bypassed'],
    ['Loaded records', eventCount],
    ['Transform runs', transformCount],
  ];

  ids.scenarioWorkbench.innerHTML = `
    <div class="scenario-hero">
      <div class="scenario-hero-copy">
        <span class="pill">${escapeHtml(summary.industry_type)}</span>
        <h3>${escapeHtml(summary.label)}</h3>
        <p>${escapeHtml(summary.description)}</p>
        <p class="note">${escapeHtml(scenarioNarrative(summary))}</p>
      </div>
      <div class="scenario-alert${auditPending ? ' is-pending' : warningCount ? ' has-warning' : ''}">
        <span>Audit readiness</span>
        <strong>${escapeHtml(verdict)}</strong>
        <small>${auditPending ? escapeHtml(readiness.detail) : `${escapeHtml(summary.reference_format)} references, ${escapeHtml(sourceCte)} source flow`}</small>
      </div>
    </div>
    <div class="scenario-signal-grid">
      ${cards
        .map(
          ([label, value]) => `
            <article class="scenario-signal-card">
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </article>
          `,
        )
        .join('')}
    </div>
    <div class="audit-checklist">
      ${
        checks.length
          ? checks
              .map(
                (item) => `
                  <article class="audit-check${item.ok ? ' is-pass' : ' is-watch'}">
                    <header>
                      <strong>${escapeHtml(item.label)}</strong>
                      <span>${item.ok ? 'Visible' : 'Not yet seen'}</span>
                    </header>
                    <p>${escapeHtml(item.detail)}</p>
                  </article>
                `,
              )
              .join('')
          : `<article class="audit-check is-watch">
              <header>
                <strong>${escapeHtml(readiness.mismatch ? 'Not scored for this line profile' : 'Backend audit pending')}</strong>
                <span>${escapeHtml(readiness.mismatch ? 'Start the line' : 'Refresh needed')}</span>
              </header>
              <p>${escapeHtml(readiness.detail)}</p>
            </article>`
      }
    </div>
  `;
}

// Everything that turns server payloads into markup.
//
// These never call an action directly. Rendered markup that needs to trigger
// one goes through `traceLotCode`, which `main.js` wires up -- see the note in
// dom.js for why.

import { DELIVERY_MODE_LABELS, DELIVERY_RECOVERY_HINTS, NEXT_ACTION_TARGETS, journey, state } from './state.js';
import { flashPanel, ids, setStatus, syncRunButtons, traceLotCode } from './dom.js';
import { cteLabel, deliveryTone, escapeHtml, formatDateTime, formatKdeValue, operationTypeLabel, scenarioLabel } from './format.js';
import { api } from './api.js';

export function renderGuide(status = state.status, events = state.events) {
  const rail = document.getElementById('guideRail');
  if (!rail) {
    return;
  }
  const hasEvents = (events || []).length > 0;
  const delivery = status?.stats?.delivery || {};
  const delivered = Number(delivery.posted || 0) > 0 || Number(delivery.failed || 0) > 0;
  const done = {
    setup: hasEvents,
    data: hasEvents,
    delivery: delivered,
    trace: journey.traced,
    export: journey.exported,
  };
  let currentAssigned = false;
  rail.querySelectorAll('[data-guide-step]').forEach((step) => {
    const key = step.dataset.guideStep;
    if (done[key]) {
      step.dataset.state = 'done';
    } else if (!currentAssigned) {
      step.dataset.state = 'current';
      currentAssigned = true;
    } else {
      step.dataset.state = '';
    }
  });
}

export function scenariosForOperation(operationType = ids.operationType.value || 'all') {
  if (operationType === 'all') {
    return state.allScenarios;
  }
  return state.allScenarios.filter((scenario) => scenario.operation_type === operationType);
}

export function renderOperationTypeOptions(scenarios, preferredOperationType = ids.operationType.value || 'all') {
  const operationTypes = ['all', ...new Set(scenarios.map((scenario) => scenario.operation_type))];
  ids.operationType.innerHTML = operationTypes
    .map(
      (operationType) => `
        <option value="${escapeHtml(operationType)}">${escapeHtml(operationTypeLabel(operationType))}</option>
      `,
    )
    .join('');
  ids.operationType.value = operationTypes.includes(preferredOperationType) ? preferredOperationType : 'all';
  updateOperationTypeDescription();
}

export function updateOperationTypeDescription() {
  const operationType = ids.operationType.value || 'all';
  if (operationType === 'all') {
    ids.operationTypeDescription.textContent =
      'Choose the type of operation you run to narrow the scenario presets to matching flows.';
    return;
  }
  const visibleCount = scenariosForOperation(operationType).length;
  ids.operationTypeDescription.textContent =
    `${operationTypeLabel(operationType)} view: showing ${visibleCount} matching scenario${visibleCount === 1 ? '' : 's'} you can start from.`;
}

export function renderScenarioOptions(
  scenarios,
  preferredScenarioId = ids.scenario.value || 'leafy_greens_supplier',
  preferredOperationType = null,
) {
  state.allScenarios = scenarios;
  state.scenarioCatalog = Object.fromEntries(scenarios.map((scenario) => [scenario.id, scenario]));
  state.scenarioLabels = Object.fromEntries(scenarios.map((scenario) => [scenario.id, scenario.label]));
  const selectedScenario = state.scenarioCatalog[preferredScenarioId] || null;
  renderOperationTypeOptions(
    scenarios,
    preferredOperationType || selectedScenario?.operation_type || ids.operationType.value || 'all',
  );
  const filteredScenarios = scenariosForOperation();
  ids.scenario.innerHTML = filteredScenarios
    .map(
      (scenario) => `
        <option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.label)}</option>
      `,
    )
    .join('');
  ids.scenario.value = filteredScenarios.some((scenario) => scenario.id === preferredScenarioId)
    ? preferredScenarioId
    : filteredScenarios[0]?.id || scenarios[0]?.id || 'leafy_greens_supplier';
  renderReadinessBanner(activeScenarioSummary(), state.events, state.status);
  renderScenarioWorkbench(state.status, state.events);
  renderRecordSpotlight(state.events);
}

export async function loadScenarios() {
  const payload = await api('/api/scenarios');
  renderScenarioOptions(payload.scenarios || []);
}

export function applyConfigToForm(config) {
  if (!config) {
    return;
  }
  ids.source.value = config.source || 'codex-simulator';
  if (ids.operationScale) {
    ids.operationScale.value = config.scale || 'midsize';
  }
  const scenarioId = config.scenario || 'leafy_greens_supplier';
  const scenario = state.scenarioCatalog[scenarioId];
  renderScenarioOptions(
    state.allScenarios.length ? state.allScenarios : Object.values(state.scenarioCatalog),
    scenarioId,
    scenario?.operation_type || null,
  );
  ids.interval.value = config.interval_seconds ?? 1.5;
  ids.batchSize.value = config.batch_size ?? 3;
  ids.seed.value = config.seed ?? '';
  ids.deliveryMode.value = config.delivery?.mode || 'mock';
  ids.endpoint.value = config.delivery?.endpoint || '';
  ids.apiKey.value = '';
  ids.tenantId.value = config.delivery?.tenant_id || '';
}

export function renderScenarioSaveOptions(saves) {
  const selected = ids.scenarioSave.value;
  state.scenarioSaves = saves || [];
  if (!state.scenarioSaves.length) {
    ids.scenarioSave.innerHTML = '<option value="">No saved scenarios</option>';
    ids.scenarioSave.value = '';
    ids.loadScenarioBtn.disabled = true;
    updateScenarioSaveDescription();
    return;
  }
  ids.scenarioSave.innerHTML = state.scenarioSaves
    .map(
      (save) => `
        <option value="${escapeHtml(save.scenario)}">${escapeHtml(save.label)}</option>
      `,
    )
    .join('');
  ids.scenarioSave.value = state.scenarioSaves.some((save) => save.scenario === selected)
    ? selected
    : state.scenarioSaves[0].scenario;
  ids.loadScenarioBtn.disabled = false;
  updateScenarioSaveDescription();
}

export async function loadScenarioSaves() {
  const payload = await api('/api/scenario-saves');
  renderScenarioSaveOptions(payload.saves || []);
}

export function updateScenarioSaveDescription() {
  const selected = ids.scenarioSave.value;
  const save = state.scenarioSaves.find((item) => item.scenario === selected);
  if (!save) {
    ids.scenarioSaveDescription.textContent = 'Save the current scenario controls and event log for later demos.';
    return;
  }
  const savedAt = new Date(save.saved_at).toLocaleString();
  ids.scenarioSaveDescription.textContent = `${save.label}: ${save.record_count} event(s), ${save.lot_codes.length} lot(s), saved ${savedAt}.`;
}

export function renderDemoFixtureOptions(fixtures) {
  const selected = ids.demoFixture.value || 'leafy_greens_trace';
  state.demoFixtureDescriptions = Object.fromEntries(
    fixtures.map((fixture) => [fixture.id, fixture.description]),
  );
  ids.demoFixture.innerHTML = fixtures
    .map(
      (fixture) => `
        <option value="${escapeHtml(fixture.id)}">${escapeHtml(fixture.label)}</option>
      `,
    )
    .join('');
  ids.demoFixture.value = state.demoFixtureDescriptions[selected] ? selected : fixtures[0]?.id || 'leafy_greens_trace';
  updateDemoFixtureDescription();
}

export async function loadDemoFixtures() {
  const payload = await api('/api/demo-fixtures');
  renderDemoFixtureOptions(payload.fixtures || []);
}

export function updateDemoFixtureDescription() {
  const fixtureId = ids.demoFixture.value || 'leafy_greens_trace';
  ids.demoFixtureDescription.textContent = state.demoFixtureDescriptions[fixtureId] || 'Deterministic demo fixture.';
}

export function renderExportPresetOptions(presets) {
  const selected = ids.exportPreset.value || 'all_records';
  state.exportPresetDescriptions = Object.fromEntries(
    presets.map((preset) => [preset.id, preset.description]),
  );
  ids.exportPreset.innerHTML = presets
    .map(
      (preset) => `
        <option value="${escapeHtml(preset.id)}">${escapeHtml(preset.label)}</option>
      `,
    )
    .join('');
  ids.exportPreset.value = state.exportPresetDescriptions[selected] ? selected : presets[0]?.id || 'all_records';
  updateExportLink();
}

export async function loadExportPresets() {
  const payload = await api('/api/mock/regengine/export/presets');
  renderExportPresetOptions(payload.presets || []);
}

export function updateExportLink() {
  const csvParams = new URLSearchParams();
  const epcisParams = new URLSearchParams();
  const preset = ids.exportPreset.value || 'all_records';
  const lotCode = ids.exportLot.value.trim();
  const startDate = ids.exportStartDate.value;
  const endDate = ids.exportEndDate.value;
  csvParams.set('preset', preset);
  if (lotCode) {
    csvParams.set('traceability_lot_code', lotCode);
    epcisParams.set('traceability_lot_code', lotCode);
  }
  if (startDate) {
    csvParams.set('start_date', startDate);
    epcisParams.set('start_date', startDate);
  }
  if (endDate) {
    csvParams.set('end_date', endDate);
    epcisParams.set('end_date', endDate);
  }
  const epcisQuery = epcisParams.toString();
  ids.exportDownloadLink.href = `/api/mock/regengine/export/fda-request?${csvParams.toString()}`;
  ids.epcisDownloadLink.href = `/api/mock/regengine/export/epcis${epcisQuery ? `?${epcisQuery}` : ''}`;
  const presetDescription = state.exportPresetDescriptions[preset] || 'FDA-request CSV export.';
  ids.exportPresetDescription.textContent = `${presetDescription} EPCIS uses the same lot and date filters.`;
}

export function preferredTraceLot(lotCodes = []) {
  return lotCodes.find((lotCode) => /OUT|TRANSFORM|FC/i.test(lotCode)) || lotCodes.at(-1) || '';
}

export function nextAction(status, events, deliveryMode) {
  const delivery = status?.stats?.delivery || {};
  if (deliveryMode === 'live') {
    return 'Verify RegEngine connection';
  }
  if (Number(delivery.retryable || 0) > 0) {
    return 'Retry failed deliveries';
  }
  if (!events.length) {
    return 'Load line data';
  }
  if (!ids.lotLookup.value.trim()) {
    return 'Trace a lot';
  }
  return 'Export evidence';
}

export function updateShellStatus(status = state.status, events = state.events, health = state.health) {
  const deliveryMode = status?.config?.delivery?.mode || ids.deliveryMode.value || 'mock';
  const build = health?.build || {};
  ids.tenantBadge.textContent = health?.tenant || 'local-demo';
  ids.deliveryModePill.textContent = DELIVERY_MODE_LABELS[deliveryMode] || deliveryMode;
  ids.deliveryModePill.dataset.tone = deliveryMode === 'live' ? 'error' : deliveryMode === 'mock' ? 'success' : 'neutral';
  ids.runStatePill.textContent = status?.running ? 'Running' : 'Idle';
  ids.runStatePill.dataset.tone = status?.running ? 'success' : 'neutral';
  syncRunButtons(Boolean(status?.running));
  ids.buildBadge.textContent = build.commit_sha_short ? `${build.version || '0.1.0'} ${build.commit_sha_short}` : build.version || '0.1.0';
  ids.liveDeliveryWarning.hidden = deliveryMode !== 'live';
  ids.nextActionText.textContent = nextAction(status, events || [], deliveryMode);
  if (ids.connectionPill) {
    ids.connectionPill.textContent =
      deliveryMode === 'live' ? 'Connected' : deliveryMode === 'mock' ? 'Sandbox' : 'Off';
    ids.connectionPill.dataset.tone = deliveryMode === 'live' ? 'error' : deliveryMode === 'mock' ? 'success' : 'neutral';
  }
}

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

export function pendingAuditModel(summary) {
  return {
    checks: [],
    score: 0,
    tone: 'watch',
    label: 'Awaiting simulator audit',
    passed: 0,
    total: 0,
    missing: 0,
    detail: summary
      ? `${summary.label} needs a simulator status refresh before audit scoring can be shown.`
      : 'Run the simulator to load backend audit scoring.',
  };
}

export function renderReadinessBanner(summary, events, status = state.status) {
  if (!summary) {
    ids.readinessBanner.innerHTML = '<p class="note">Readiness scoring will appear once scenario metadata loads.</p>';
    return;
  }
  const readiness = backendAudit(status, summary) || pendingAuditModel(summary);
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
      </div>
    </div>
  `;
}

// Warnings arrive as {field, message, severity, citation}. The events table has
// room for one, so required-tier warnings sort ahead of recommended ones: a
// missing transformation input lot is a rule violation, a missing
// reference_document_type is a nicety, and the collapsed cell should show the
// former. Older payloads without a severity are treated as recommended.
export function recordWarnings(record, summary, status = state.status) {
  const audit = backendAudit(status, summary);
  const warningPayload = audit?.warnings_by_record?.[record.record_id];
  if (!Array.isArray(warningPayload) || !warningPayload.length) {
    return [];
  }
  return warningPayload
    .filter((warning) => typeof warning?.message === 'string' && warning.message)
    .map((warning) => ({
      message: warning.message,
      severity: warning.severity === 'required' ? 'required' : 'recommended',
    }))
    .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'required' ? -1 : 1));
}

export function renderScenarioWorkbench(status = state.status, events = state.events) {
  const summary = activeScenarioSummary();
  if (!summary) {
    ids.scenarioWorkbench.innerHTML = '<p class="note">Scenario metadata will appear here once presets load.</p>';
    return;
  }
  const readiness = backendAudit(status, summary) || pendingAuditModel(summary);
  const checks = readiness.checks;
  const sourceCte = sourceCteForScenario(summary);
  const eventCount = (events || []).length;
  const warningCount = readiness.missing;
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
      <div class="scenario-alert${warningCount ? ' has-warning' : ''}">
        <span>Audit readiness</span>
        <strong>${warningCount ? `${warningCount} signal(s) still missing` : 'Signals visible'}</strong>
        <small>${escapeHtml(summary.reference_format)} references, ${escapeHtml(sourceCte)} source flow</small>
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
                <strong>Backend audit pending</strong>
                <span>Refresh needed</span>
              </header>
              <p>${escapeHtml(readiness.detail)}</p>
            </article>`
      }
    </div>
  `;
}

export function renderStats(status) {
  const stats = status?.stats || {};
  const engine = stats.engine || {};
  const scenarioId = status?.config?.scenario || ids.scenario.value;
  const health = state.health || {};
  const auth = health.auth || {};
  const authState = auth.enabled ? 'Enabled' : 'Off';
  const storageScope = auth.uses_default_storage === false ? 'Tenant' : 'Local';
  const cards = [
    ['Loop status', status?.running ? 'Running' : 'Stopped'],
    ['Scenario', scenarioLabel(scenarioId)],
    ['Operation size', engine.scale ?? 'midsize'],
    ['Facilities', engine.locations ?? '—'],
    ['Total records', stats.total_records ?? 0],
    ['Unique lots', stats.unique_lots ?? 0],
    ['Auth', authState],
    ['Storage', storageScope],
    ['Persist path', stats.persist_path ?? 'data/events.jsonl'],
    ['Harvested queue', engine.harvested ?? 0],
    ['In transit', engine.in_transit ?? 0],
    ['Processor inventory', engine.processor_inventory ?? 0],
    ['Retail inventory', engine.retail_inventory ?? 0],
  ];
  ids.statsGrid.innerHTML = cards
    .map(
      ([label, value]) => `
        <article class="stat-card">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </article>
      `,
    )
    .join('');
}

export function renderDeliverySummary(status) {
  const delivery = status?.stats?.delivery || {};
  const retryable = Number(delivery.retryable || 0);
  ids.retryFailedBtn.disabled = retryable < 1;
  const cards = [
    ['Posted', delivery.posted ?? 0, 'success'],
    ['Failed', delivery.failed ?? 0, retryable > 0 ? 'error' : 'neutral'],
    ['Generated only', delivery.generated ?? 0, 'neutral'],
    ['Attempts', delivery.attempts ?? 0, 'neutral'],
  ];
  const lastAttempt = delivery.last_attempt_at ? new Date(delivery.last_attempt_at).toLocaleString() : 'No attempts yet';
  const lastSuccess = delivery.last_success_at ? new Date(delivery.last_success_at).toLocaleString() : 'No successful delivery yet';
  const recoveryHint = deliveryRecoveryHint(state.events);
  ids.deliverySummary.innerHTML = `
    <div class="delivery-cards">
      ${cards
        .map(
          ([label, value, tone]) => `
            <article class="delivery-card" data-tone="${escapeHtml(tone)}">
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </article>
          `,
        )
        .join('')}
    </div>
    <dl class="delivery-details">
      <div>
        <dt>Last attempt</dt>
        <dd>${escapeHtml(lastAttempt)}</dd>
      </div>
      <div>
        <dt>Last success</dt>
        <dd>${escapeHtml(lastSuccess)}</dd>
      </div>
      ${
        delivery.last_error
          ? `
            <div>
              <dt>Last error</dt>
              <dd data-tone="error">${escapeHtml(delivery.last_error)}</dd>
            </div>
          `
          : ''
      }
      ${
        recoveryHint
          ? `
            <div>
              <dt>How to recover</dt>
              <dd>${escapeHtml(recoveryHint)}</dd>
            </div>
          `
          : ''
      }
    </dl>
  `;
}

export function deliveryRecoveryHint(events = []) {
  const failedRecord = (events || []).find((record) => record.delivery_status === 'failed');
  if (!failedRecord) {
    return '';
  }
  const statusCode = failedRecord.delivery_metadata?.status_code;
  if (statusCode && DELIVERY_RECOVERY_HINTS[statusCode]) {
    return DELIVERY_RECOVERY_HINTS[statusCode];
  }
  if ((failedRecord.error || '').includes('Missing required KDEs')) {
    return 'RegEngine rejected the record for missing KDEs. Fix the source data (the errors name the exact fields), then retry or re-import.';
  }
  if ((failedRecord.error || '').includes('Duplicate event')) {
    return 'RegEngine saw a duplicate of this event in the same batch — it was already recorded.';
  }
  return 'Check the error detail, fix the cause, then use Retry failed. Retries reuse the original idempotency key so nothing double-ingests.';
}

export function pickSpotlightRecord(events) {
  if (!events.length) {
    return null;
  }
  return (
    events.find((record) => record.event.cte_type === 'transformation') ||
    events.find((record) => record.event.cte_type === 'shipping') ||
    events[0]
  );
}

export function spotlightFields(event) {
  const preferredKeys = [
    'reference_document',
    'reference_document_number',
    'vessel_identifier',
    'vessel_name',
    'landing_date',
    'field_gps_coordinates',
    'plu_code',
    'flow_type',
    'silo_identifier',
    'vat_identifier',
    'packaging_hierarchy',
    'packaging_conversion',
    'input_traceability_lot_codes',
    'output_traceability_lot_codes',
    'rework_traceability_lot_codes',
    'yield_ratio',
    'sscc',
  ];
  const entries = [];
  for (const key of preferredKeys) {
    if (Object.prototype.hasOwnProperty.call(event.kdes || {}, key)) {
      entries.push([key, event.kdes[key]]);
    }
  }
  if (!entries.length) {
    return Object.entries(event.kdes || {}).slice(0, 8);
  }
  return entries.slice(0, 8);
}

export function renderRecordSpotlight(events) {
  const record = pickSpotlightRecord(events || []);
  if (!record) {
    ids.recordSpotlight.innerHTML = '<p class="note">Run the pipeline or load a fixture to inspect an audit-style record spotlight.</p>';
    return;
  }
  const event = record.event;
  const keyFacts = [
    ['CTE', cteLabel(event.cte_type)],
    ['Lot', event.traceability_lot_code],
    ['Quantity', `${event.quantity} ${event.unit_of_measure}`],
    ['Location', event.location_name],
    ['Posted status', record.delivery_status],
  ];
  const fields = spotlightFields(event);
  ids.recordSpotlight.innerHTML = `
    <div class="spotlight-hero">
      <div>
        <span class="pill">${escapeHtml(cteLabel(event.cte_type))}</span>
        <h3>${escapeHtml(event.product_description)}</h3>
        <p class="note">Sequence ${escapeHtml(record.sequence_no)} at ${formatDateTime(event.timestamp)}</p>
      </div>
      <button class="button secondary" type="button" data-spotlight-lot="${escapeHtml(event.traceability_lot_code)}">Trace this lot</button>
    </div>
    <div class="spotlight-facts">
      ${keyFacts
        .map(
          ([label, value]) => `
            <article class="spotlight-fact">
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </article>
          `,
        )
        .join('')}
    </div>
    <div class="spotlight-kdes">
      ${fields
        .map(
          ([key, value]) => `
            <article class="spotlight-kde">
              <span>${escapeHtml(key)}</span>
              <strong>${escapeHtml(formatKdeValue(value))}</strong>
            </article>
          `,
        )
        .join('')}
    </div>
  `;
  ids.recordSpotlight.querySelector('[data-spotlight-lot]')?.addEventListener('click', async (eventNode) => {
    await traceLotCode(eventNode.currentTarget.dataset.spotlightLot);
  });
}

export function renderEvents(events) {
  const summary = activeScenarioSummary();
  if (!events.length) {
    ids.eventsBody.innerHTML = `
      <tr>
        <td colspan="9" class="empty-state">No events yet. Load a fixture or run a single batch.</td>
      </tr>
    `;
    return;
  }
  ids.eventsBody.innerHTML = events
    .map((record) => {
      const event = record.event;
      const warnings = recordWarnings(record, summary);
      return `
        <tr class="${warnings.length ? 'has-audit-warning' : ''}">
          <td>${record.sequence_no}</td>
          <td><span class="pill">${escapeHtml(event.cte_type)}</span></td>
          <td><button class="link-button" data-lot="${escapeHtml(event.traceability_lot_code)}">${escapeHtml(event.traceability_lot_code)}</button></td>
          <td>${escapeHtml(event.product_description)}</td>
          <td>${escapeHtml(event.location_name)}</td>
          <td>${escapeHtml(new Date(event.timestamp).toLocaleString())}</td>
          <td>${escapeHtml(record.destination_mode)}</td>
          <td>
            ${escapeHtml(record.delivery_attempts || 0)}
            ${warnings.length ? `<small class="status-warning" data-severity="${escapeHtml(warnings[0].severity)}">${escapeHtml(warnings[0].message)}</small>` : ''}
          </td>
          <td>
            <span class="status-pill" data-tone="${escapeHtml(deliveryTone(record.delivery_status))}">${escapeHtml(record.delivery_status)}</span>
            ${record.error ? `<small class="status-error">${escapeHtml(record.error)}</small>` : ''}
          </td>
        </tr>
      `;
    })
    .join('');

  ids.eventsBody.querySelectorAll('[data-lot]').forEach((button) => {
    button.addEventListener('click', async () => {
      await traceLotCode(button.dataset.lot);
    });
  });
}

export function renderLineage(payload, traceabilityLotCode) {
  const records = payload.records || [];
  const scenarioSummary = activeScenarioSummary();
  if (!records.length) {
    ids.lineageResults.innerHTML = `<p class="note">No lineage found for ${escapeHtml(traceabilityLotCode)}.</p>`;
    return;
  }
  const nodes = payload.nodes || [];
  const edges = payload.edges || [];
  const nodeByLot = new Map(nodes.map((node) => [node.lot_code, node]));
  const locations = new Set(records.map((record) => record.event.location_name));
  const transformations = records.filter((record) => record.event.cte_type === 'transformation').length;
  const queriedNode = nodeByLot.get(traceabilityLotCode);
  const stats = [
    ['Lots', nodes.length || new Set(records.map((record) => record.event.traceability_lot_code)).size],
    ['Events', records.length],
    ['Links', edges.length],
    ['Transformations', transformations],
  ];

  const lineageSummary = queriedNode
    ? `
      <div class="lineage-focus">
        <span>Focused lot</span>
        <strong>${escapeHtml(queriedNode.lot_code)}</strong>
        <p>${escapeHtml(queriedNode.product_description)}</p>
      </div>
    `
    : '';

  const statMarkup = stats
    .map(
      ([label, value]) => `
        <div class="lineage-stat">
          <span>${label}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `,
    )
    .join('');

  const nodeMarkup = nodes
    .map(
      (node) => `
        <article class="lineage-lot${node.lot_code === traceabilityLotCode ? ' is-current' : ''}">
          <header>
            <span>${escapeHtml(node.lot_code)}</span>
            <strong>${escapeHtml(node.event_count)} event(s)</strong>
          </header>
          <p>${escapeHtml(node.product_description)}</p>
          <small>${escapeHtml((node.cte_types || []).map(cteLabel).join(' -> '))}</small>
          <small>${escapeHtml((node.locations || []).join(' -> '))}</small>
        </article>
      `,
    )
    .join('');

  const edgeMarkup = edges.length
    ? edges
        .map((edge) => {
          const source = nodeByLot.get(edge.source_lot_code);
          const target = nodeByLot.get(edge.target_lot_code);
          return `
            <li>
              <button class="link-button" data-lineage-lot="${escapeHtml(edge.source_lot_code)}">
                ${escapeHtml(source?.product_description || edge.source_lot_code)}
              </button>
              <span class="flow-arrow">-&gt;</span>
              <button class="link-button" data-lineage-lot="${escapeHtml(edge.target_lot_code)}">
                ${escapeHtml(target?.product_description || edge.target_lot_code)}
              </button>
              <span class="flow-meta">${escapeHtml(cteLabel(edge.cte_type))}</span>
            </li>
          `;
        })
        .join('')
    : `<li class="note">This lot has a same-lot timeline with no downstream output links yet.</li>`;

  const timelineMarkup = records
    .map((record) => {
      const event = record.event;
      const warnings = recordWarnings(record, scenarioSummary);
      const kdes = Object.entries(event.kdes || {})
        .slice(0, 6)
        .map(([key, value]) => `<li><strong>${escapeHtml(key)}:</strong> ${escapeHtml(formatKdeValue(value))}</li>`)
        .join('');
      return `
        <article class="lineage-card${warnings.length ? ' has-audit-warning' : ''}">
          <header>
            <h3>${escapeHtml(cteLabel(event.cte_type))}</h3>
            <span>${formatDateTime(event.timestamp)}</span>
          </header>
          <p><strong>Lot:</strong> ${escapeHtml(event.traceability_lot_code)}</p>
          <p><strong>Product:</strong> ${escapeHtml(event.product_description)}</p>
          <p><strong>Location:</strong> ${escapeHtml(event.location_name)}</p>
          ${warnings.length ? `<p class="lineage-warning" data-severity="${escapeHtml(warnings[0].severity)}">${escapeHtml(warnings.map((warning) => warning.message).join(' • '))}</p>` : ''}
          <ul>${kdes}</ul>
        </article>
      `;
    })
    .join('');

  ids.lineageResults.innerHTML = `
    <div class="lineage-overview">
      ${lineageSummary}
      <div class="lineage-stats">${statMarkup}</div>
      <p class="note">${escapeHtml(locations.size)} location(s) represented in this lineage trace.</p>
    </div>
    <div class="lineage-flow">
      <h3>Lot flow</h3>
      <div class="lineage-lots">${nodeMarkup}</div>
      <ul>${edgeMarkup}</ul>
    </div>
    <div class="lineage-timeline">
      <h3>Event timeline</h3>
      <div class="lineage-cards">${timelineMarkup}</div>
    </div>
  `;

  ids.lineageResults.querySelectorAll('[data-lineage-lot]').forEach((button) => {
    button.addEventListener('click', async () => {
      await traceLotCode(button.dataset.lineageLot);
    });
  });
}

export function renderImportResult(result) {
  const tone = result.status === 'accepted' ? 'success' : result.status === 'delivery_failed' ? 'error' : 'neutral';
  const errors = (result.errors || []).slice(0, 8);
  const warnings = (result.warnings || []).slice(0, 8);
  const errorList = errors
    .map((error) => {
      const field = error.field ? ` ${escapeHtml(error.field)}:` : '';
      return `<li>Row ${escapeHtml(error.row)}${field} ${escapeHtml(error.message)}</li>`;
    })
    .join('');
  const warningList = warnings
    .map((warning) => {
      const field = warning.field ? ` ${escapeHtml(warning.field)}:` : '';
      return `<li>Row ${escapeHtml(warning.row)}${field} ${escapeHtml(warning.message)}</li>`;
    })
    .join('');
  ids.importResults.innerHTML = `
    <div class="import-summary" data-tone="${escapeHtml(tone)}">
      Accepted ${escapeHtml(result.accepted)} of ${escapeHtml(result.total)} row(s).
      Stored ${escapeHtml(result.stored)}; posted ${escapeHtml(result.posted)}; rejected ${escapeHtml(result.rejected)}.
      ${result.error ? `<span>${escapeHtml(result.error)}</span>` : ''}
    </div>
    ${errorList ? `<ul>${errorList}</ul>` : ''}
    ${warningList ? `<ul>${warningList}</ul>` : ''}
  `;
}

export function renderSnapshot(status, events, health = state.health) {
  state.status = status;
  state.health = health;
  state.events = events;
  updateShellStatus(status, events, health);
  renderGuide(status, events);
  renderReadinessBanner(activeScenarioSummary(), events, status);
  renderStats(status);
  renderDeliverySummary(status);
  renderScenarioWorkbench(status, events);
  renderRecordSpotlight(events);
  renderEvents(events);
  if (Date.now() >= state.statusHoldUntil) {
    setStatus(status.running ? 'Production line is running.' : 'Production line is idle.');
  }
}

export function renderConnectionStatus(integration) {
  if (!ids.connectionChips || !integration) {
    return;
  }
  const chips = [
    ['Endpoint', integration.endpoint_host || 'default'],
    ['API key', integration.api_key_configured ? 'Configured' : 'Not set'],
    ['Tenant', integration.tenant_configured ? 'Configured' : 'Not set'],
    ['HMAC signing', integration.hmac_configured ? 'On' : 'Off'],
    ['Contract', integration.contract_version ? `v${integration.contract_version}` : 'n/a'],
  ];
  ids.connectionChips.innerHTML = chips
    .map(
      ([label, value]) => `
        <span class="connection-chip">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </span>
      `,
    )
    .join('');
  if (Array.isArray(integration.mock_friction)) {
    if (ids.frictionInvalidKey) ids.frictionInvalidKey.checked = integration.mock_friction.includes('invalid_key');
    if (ids.frictionSubscription) ids.frictionSubscription.checked = integration.mock_friction.includes('subscription_inactive');
    if (ids.frictionRateLimit) ids.frictionRateLimit.checked = integration.mock_friction.includes('rate_limit');
  }
}

export async function loadIntegrationStatus() {
  const integration = await api('/api/integration/status');
  renderConnectionStatus(integration);
}

export function goToNextAction() {
  const label = ids.nextActionText.textContent.trim();
  flashPanel(NEXT_ACTION_TARGETS[label] || '.run-panel');
  if (label === 'Trace a lot') {
    ids.lotLookup.focus();
  }
}

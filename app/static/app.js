const state = {
  status: null,
  health: null,
  events: [],
  allScenarios: [],
  scenarioCatalog: {},
  eventSource: null,
  fallbackTimer: null,
  reconnectTimer: null,
  reconnectDelayMs: 0,
  statusHoldUntil: 0,
  scenarioLabels: {
    leafy_greens_supplier: 'Leafy greens supplier',
    fresh_cut_processor: 'Fresh-cut processor',
    retailer_readiness_demo: 'Retailer readiness demo',
    seafood_first_receiver: 'Seafood first receiver',
    dairy_continuous_flow: 'Dairy continuous flow',
  },
  demoFixtureDescriptions: {
    leafy_greens_trace: 'Harvest through cooling, packout, shipment, and DC receipt for one leafy greens lot.',
  },
  scenarioSaves: [],
  operationTypeLabels: {
    all: 'All operations',
    supplier: 'Supplier',
    processor: 'Processor',
    retailer: 'Retail / distribution',
    first_receiver: 'First receiver',
    copacker: 'Co-packer / contract manufacturer',
    distributor: 'Distributor / wholesaler',
    foodservice: 'Foodservice',
    egg_producer: 'Egg producer',
  },
  exportPresetDescriptions: {
    all_records: 'Full FDA-request export for the selected date range.',
  },
};

const DEFAULT_LIVE_INGEST_ENDPOINT = 'https://www.regengine.co/api/v1/webhooks/ingest';

const ids = {
  source: document.getElementById('source'),
  operationType: document.getElementById('operationType'),
  operationScale: document.getElementById('operationScale'),
  scenario: document.getElementById('scenario'),
  interval: document.getElementById('interval'),
  batchSize: document.getElementById('batchSize'),
  seed: document.getElementById('seed'),
  deliveryMode: document.getElementById('deliveryMode'),
  endpoint: document.getElementById('endpoint'),
  apiKey: document.getElementById('apiKey'),
  tenantId: document.getElementById('tenantId'),
  csvImportType: document.getElementById('csvImportType'),
  csvFile: document.getElementById('csvFile'),
  importResults: document.getElementById('importResults'),
  scenarioSave: document.getElementById('scenarioSave'),
  scenarioSaveDescription: document.getElementById('scenarioSaveDescription'),
  saveScenarioBtn: document.getElementById('saveScenarioBtn'),
  loadScenarioBtn: document.getElementById('loadScenarioBtn'),
  demoFixture: document.getElementById('demoFixture'),
  demoFixtureDescription: document.getElementById('demoFixtureDescription'),
  loadFixtureBtn: document.getElementById('loadFixtureBtn'),
  operationTypeDescription: document.getElementById('operationTypeDescription'),
  exportPreset: document.getElementById('exportPreset'),
  exportLot: document.getElementById('exportLot'),
  exportStartDate: document.getElementById('exportStartDate'),
  exportEndDate: document.getElementById('exportEndDate'),
  exportDownloadLink: document.getElementById('exportDownloadLink'),
  epcisDownloadLink: document.getElementById('epcisDownloadLink'),
  exportPresetDescription: document.getElementById('exportPresetDescription'),
  statusMessage: document.getElementById('statusMessage'),
  nextActionText: document.getElementById('nextActionText'),
  tenantBadge: document.getElementById('tenantBadge'),
  deliveryModePill: document.getElementById('deliveryModePill'),
  buildBadge: document.getElementById('buildBadge'),
  liveDeliveryWarning: document.getElementById('liveDeliveryWarning'),
  runStatePill: document.getElementById('runStatePill'),
  statsGrid: document.getElementById('statsGrid'),
  deliverySummary: document.getElementById('deliverySummary'),
  retryFailedBtn: document.getElementById('retryFailedBtn'),
  eventsBody: document.getElementById('eventsBody'),
  lotLookup: document.getElementById('lotLookup'),
  lineageResults: document.getElementById('lineageResults'),
  readinessBanner: document.getElementById('readinessBanner'),
  scenarioWorkbench: document.getElementById('scenarioWorkbench'),
  recordSpotlight: document.getElementById('recordSpotlight'),
  connectionPill: document.getElementById('connectionPill'),
  connectionChips: document.getElementById('connectionChips'),
  connectionResult: document.getElementById('connectionResult'),
  testConnectionBtn: document.getElementById('testConnectionBtn'),
  saveIntegrationBtn: document.getElementById('saveIntegrationBtn'),
  frictionInvalidKey: document.getElementById('frictionInvalidKey'),
  frictionSubscription: document.getElementById('frictionSubscription'),
  frictionRateLimit: document.getElementById('frictionRateLimit'),
  startBtn: document.getElementById('startBtn'),
  stopBtn: document.getElementById('stopBtn'),
  stepBtn: document.getElementById('stepBtn'),
  replayBtn: document.getElementById('replayBtn'),
  resetBtn: document.getElementById('resetBtn'),
  refreshBtn: document.getElementById('refreshBtn'),
  lineageBtn: document.getElementById('lineageBtn'),
  importCsvBtn: document.getElementById('importCsvBtn'),
  loadFixtureBtn: document.getElementById('loadFixtureBtn'),
};

// Keep Start/Pause honest about the loop state. Buttons mid-request keep
// their busy-disabled state until the request settles.
function syncRunButtons(running = Boolean(state.status?.running)) {
  if (ids.startBtn.dataset.busy !== '1') {
    ids.startBtn.disabled = running;
  }
  if (ids.stopBtn.dataset.busy !== '1') {
    ids.stopBtn.disabled = !running;
  }
}

// Runs an async handler with the button disabled and spinning so double-clicks
// can't fire duplicate requests.
async function runWithBusy(button, handler) {
  if (button.dataset.busy === '1') {
    return;
  }
  button.dataset.busy = '1';
  button.classList.add('is-busy');
  button.disabled = true;
  try {
    await handler();
  } finally {
    delete button.dataset.busy;
    button.classList.remove('is-busy');
    button.disabled = false;
    syncRunButtons();
  }
}

function bindAsyncClick(button, handler) {
  button?.addEventListener('click', () => runWithBusy(button, handler));
}

const DELIVERY_MODE_LABELS = {
  mock: 'sandbox',
  live: 'connected',
  none: 'off',
};

// Recovery guidance keyed by the HTTP status a delivery failure carried —
// the same failure vocabulary a live RegEngine integration produces.
const DELIVERY_RECOVERY_HINTS = {
  401: 'RegEngine rejected the API key. Fix the key in RegEngine connection settings, then retry.',
  402: 'The RegEngine subscription for this tenant is inactive. Reactivate billing, then retry — the retry reuses the original idempotency key, so nothing double-ingests.',
  403: 'RegEngine refused the request. Check that the tenant ID matches the API key and the key has the webhooks.ingest scope.',
  422: 'RegEngine rejected the request shape (for example, more than 500 events in one batch).',
  429: 'RegEngine is rate limiting this tenant. Wait for the window to pass, then retry.',
};

function frictionSelections() {
  return [ids.frictionInvalidKey, ids.frictionSubscription, ids.frictionRateLimit]
    .filter((checkbox) => checkbox && checkbox.checked)
    .map((checkbox) => checkbox.value);
}

// Progress through the guided flow, used to light up the "How to use this
// console" stepper. traced/exported are set by user actions; the rest are
// derived from live state on every snapshot.
const journey = { traced: false, exported: false };

function renderGuide(status = state.status, events = state.events) {
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
      step.removeAttribute('aria-current');
    } else if (!currentAssigned) {
      step.dataset.state = 'current';
      step.setAttribute('aria-current', 'step');
      currentAssigned = true;
    } else {
      step.dataset.state = '';
      step.removeAttribute('aria-current');
    }
  });
}

function flashPanel(selector) {
  const panel = document.querySelector(selector);
  if (!panel) {
    return;
  }
  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
  panel.classList.remove('panel-flash');
  // Force a reflow so re-adding the class restarts the animation.
  void panel.offsetWidth;
  panel.classList.add('panel-flash');
}

// --- Onboarding: first-run welcome + guided tour -------------------------
// Seen-state lives in localStorage so the welcome only interrupts once per
// browser; private-mode storage failures degrade to "never show".
const ONBOARDING_KEY = 'inflowLab.onboarded.v1';

function onboardingSeen() {
  try {
    return window.localStorage.getItem(ONBOARDING_KEY) === 'done';
  } catch {
    return true;
  }
}

function markOnboarded() {
  try {
    window.localStorage.setItem(ONBOARDING_KEY, 'done');
  } catch {
    // Storage unavailable — the welcome simply shows again next visit.
  }
}

// Fuller copy than the guide rail's one-liners: each tour stop explains what
// the panel is for and what to actually click.
const TOUR_STEPS = [
  {
    target: '.compact-panel',
    title: 'Set up the production line',
    body: 'This panel decides what the plant makes. The line profile picks products, locations, and event flow; the operation size scales lot counts and site counts. The defaults are fine for a first run — you can change them any time.',
  },
  {
    target: '.run-panel',
    title: 'Bring in traceability data',
    body: 'Start line records events continuously, Record next batch (in the header) does one batch at a time, and Load today’s line data fills the shift with a ready-made trace instantly. Clear shift wipes the workspace when you want a fresh start.',
  },
  {
    target: '.delivery-monitor',
    title: 'Watch delivery to RegEngine',
    body: 'Every recorded event posts to RegEngine — the built-in sandbox until you connect a real workspace in the Integrations panel. Failures land here with recovery guidance, and Retry failed is always safe: retries reuse the original idempotency key.',
  },
  {
    target: '.lineage-panel',
    title: 'Trace a lot through the plant',
    body: 'Click any lot code in the shift log — or type one into the trace box at the top — to follow it backward to its origin and forward to wherever it went, including through transformations.',
  },
  {
    target: '.evidence-panel',
    title: 'Export audit evidence',
    body: 'When the shift log has records, download the FDA-request CSV (header button) or the EPCIS 2.0 JSON package. The filters narrow the export by preset, lot, or date range. That’s the whole loop — you’re ready to run it yourself.',
  },
];

const tour = { active: false, index: 0 };

const tourEls = {
  popover: document.getElementById('tourPopover'),
  progress: document.getElementById('tourProgress'),
  title: document.getElementById('tourTitle'),
  body: document.getElementById('tourBody'),
  back: document.getElementById('tourBackBtn'),
  next: document.getElementById('tourNextBtn'),
  skip: document.getElementById('tourSkipBtn'),
};

function clearTourHighlight() {
  document.querySelectorAll('.tour-highlight').forEach((node) => node.classList.remove('tour-highlight'));
}

function showTourStep(index) {
  tour.index = Math.min(Math.max(index, 0), TOUR_STEPS.length - 1);
  const step = TOUR_STEPS[tour.index];
  tourEls.progress.textContent = `Step ${tour.index + 1} of ${TOUR_STEPS.length}`;
  tourEls.title.textContent = step.title;
  tourEls.body.textContent = step.body;
  tourEls.back.disabled = tour.index === 0;
  tourEls.next.textContent = tour.index === TOUR_STEPS.length - 1 ? 'Finish' : 'Next';
  clearTourHighlight();
  const target = document.querySelector(step.target);
  if (target) {
    target.classList.add('tour-highlight');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function startTour() {
  tour.active = true;
  tourEls.popover.hidden = false;
  showTourStep(0);
  tourEls.next.focus();
}

function endTour() {
  tour.active = false;
  tourEls.popover.hidden = true;
  clearTourHighlight();
  markOnboarded();
  document.getElementById('tourBtn')?.focus();
}

function advanceTour(delta) {
  const next = tour.index + delta;
  if (next >= TOUR_STEPS.length) {
    endTour();
    setStatus('Tour finished — load line data or record a batch to see it all in action.', 'success', 5000);
    return;
  }
  showTourStep(next);
}

const welcomeEls = {
  overlay: document.getElementById('welcomeOverlay'),
  tour: document.getElementById('welcomeTourBtn'),
  sample: document.getElementById('welcomeSampleBtn'),
  skip: document.getElementById('welcomeSkipBtn'),
};

function hideWelcome() {
  welcomeEls.overlay.hidden = true;
  markOnboarded();
}

function maybeShowWelcome() {
  if (onboardingSeen()) {
    return;
  }
  welcomeEls.overlay.hidden = false;
  welcomeEls.tour.focus();
}

// Where "Take me there" should send the user for each next-action label.
const NEXT_ACTION_TARGETS = {
  'Verify RegEngine connection': '.integration-panel',
  'Retry failed deliveries': '.delivery-monitor',
  'Load line data': '.run-panel',
  'Trace a lot': '.lineage-panel',
  'Export evidence': '.evidence-panel',
};

function goToNextAction() {
  const label = ids.nextActionText.textContent.trim();
  flashPanel(NEXT_ACTION_TARGETS[label] || '.run-panel');
  if (label === 'Trace a lot') {
    ids.lotLookup.focus();
  }
}

function setStatus(message, tone = 'neutral', holdMs = 0) {
  // #statusMessage is a live region (see index.html), so only touch its text
  // when the message actually changes — renderSnapshot() calls this on every
  // poll/stream tick, and re-writing identical text would re-announce it.
  if (ids.statusMessage.textContent !== message) {
    ids.statusMessage.textContent = message;
  }
  ids.statusMessage.dataset.tone = tone;
  ids.statusMessage.setAttribute('aria-live', tone === 'error' ? 'assertive' : 'polite');
  state.statusHoldUntil = holdMs > 0 ? Date.now() + holdMs : 0;
}

function buildConfig() {
  const endpoint = ids.endpoint.value.trim() || DEFAULT_LIVE_INGEST_ENDPOINT;
  const apiKey = ids.apiKey.value.trim();
  const tenantId = ids.tenantId.value.trim();
  const seedValue = ids.seed.value.trim();
  return {
    source: ids.source.value.trim() || 'codex-simulator',
    scenario: ids.scenario.value,
    scale: ids.operationScale?.value || 'midsize',
    interval_seconds: Number(ids.interval.value || 1.5),
    batch_size: Number(ids.batchSize.value || 3),
    seed: seedValue === '' ? null : Number(seedValue),
    persist_path: 'data/events.jsonl',
    delivery: {
      mode: ids.deliveryMode.value,
      endpoint: endpoint || null,
      api_key: apiKey || null,
      tenant_id: tenantId || null,
      mock_friction: frictionSelections(),
    },
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return response.json();
  }
  return response.text();
}

function scenarioLabel(scenarioId) {
  return state.scenarioLabels[scenarioId] || scenarioId || 'Unknown';
}

function operationTypeLabel(operationType) {
  return state.operationTypeLabels[operationType] || operationType || 'Unknown';
}

function scenariosForOperation(operationType = ids.operationType.value || 'all') {
  if (operationType === 'all') {
    return state.allScenarios;
  }
  return state.allScenarios.filter((scenario) => scenario.operation_type === operationType);
}

function renderOperationTypeOptions(scenarios, preferredOperationType = ids.operationType.value || 'all') {
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

function updateOperationTypeDescription() {
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

function renderScenarioOptions(
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

async function loadScenarios() {
  const payload = await api('/api/scenarios');
  renderScenarioOptions(payload.scenarios || []);
}

function applyConfigToForm(config) {
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

function renderScenarioSaveOptions(saves) {
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

async function loadScenarioSaves() {
  const payload = await api('/api/scenario-saves');
  renderScenarioSaveOptions(payload.saves || []);
}

function updateScenarioSaveDescription() {
  const selected = ids.scenarioSave.value;
  const save = state.scenarioSaves.find((item) => item.scenario === selected);
  if (!save) {
    ids.scenarioSaveDescription.textContent = 'Save the current scenario controls and event log for later demos.';
    return;
  }
  const savedAt = new Date(save.saved_at).toLocaleString();
  ids.scenarioSaveDescription.textContent = `${save.label}: ${save.record_count} event(s), ${save.lot_codes.length} lot(s), saved ${savedAt}.`;
}

function renderDemoFixtureOptions(fixtures) {
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

async function loadDemoFixtures() {
  const payload = await api('/api/demo-fixtures');
  renderDemoFixtureOptions(payload.fixtures || []);
}

function updateDemoFixtureDescription() {
  const fixtureId = ids.demoFixture.value || 'leafy_greens_trace';
  ids.demoFixtureDescription.textContent = state.demoFixtureDescriptions[fixtureId] || 'Deterministic demo fixture.';
}

function renderExportPresetOptions(presets) {
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

async function loadExportPresets() {
  const payload = await api('/api/mock/regengine/export/presets');
  renderExportPresetOptions(payload.presets || []);
}

function updateExportLink() {
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

function preferredTraceLot(lotCodes = []) {
  return lotCodes.find((lotCode) => /OUT|TRANSFORM|FC/i.test(lotCode)) || lotCodes.at(-1) || '';
}

function nextAction(status, events, deliveryMode) {
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

function updateShellStatus(status = state.status, events = state.events, health = state.health) {
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

function activeScenarioSummary() {
  return state.scenarioCatalog[ids.scenario.value] || null;
}

function backendAudit(status = state.status, summary = activeScenarioSummary()) {
  const audit = status?.stats?.audit || null;
  const statusScenario = status?.config?.scenario;
  if (!audit || !summary || !statusScenario || statusScenario !== summary.id) {
    return null;
  }
  return audit;
}

function sourceCteForScenario(summary) {
  if (summary?.industry_type === 'seafood') {
    return 'First land-based receiving';
  }
  return 'Harvesting';
}

function scenarioNarrative(summary) {
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

// The stand-in used when backendAudit() has nothing to report. `missing: 0`
// is literally true -- nothing was evaluated, so nothing is missing -- but it
// is NOT the same fact as "the backend scored this profile and found no gaps".
// `pending: true` is what callers branch on so a not-computed audit can never
// render as a passing one (#193).
//
// `mismatched` separates the two ways an audit can be absent, which need
// different copy: nothing has run yet, versus something ran but scored a
// DIFFERENT profile than the one on screen. The second is the more misleading
// state, because a score does exist -- it just is not this profile's.
function pendingAuditModel(summary, status = state.status) {
  const scoredScenario = status?.config?.scenario || null;
  const mismatched = Boolean(scoredScenario && summary && scoredScenario !== summary.id);
  return {
    checks: [],
    score: 0,
    tone: 'watch',
    label: mismatched ? 'Not scored for this profile' : 'Awaiting simulator audit',
    passed: 0,
    total: 0,
    missing: 0,
    pending: true,
    mismatched,
    detail: mismatched
      ? `Showing ${summary.label}, but the backend last scored ${scenarioLabel(scoredScenario)}. Start the line on this profile to score it.`
      : summary
        ? `${summary.label} needs a simulator status refresh before audit scoring can be shown.`
        : 'Run the simulator to load backend audit scoring.',
  };
}

function renderReadinessBanner(summary, events, status = state.status) {
  if (!summary) {
    ids.readinessBanner.innerHTML = '<p class="note">Readiness scoring will appear once scenario metadata loads.</p>';
    return;
  }
  const readiness = backendAudit(status, summary) || pendingAuditModel(summary, status);
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

// Returns both the warnings AND whether an audit ran at all. These are
// different facts -- a row nothing evaluated is not a row the backend cleared
// (#193) -- and they used to be carried as null-or-array, which every caller
// had to remember to unpack in that order. An explicit `evaluated` flag makes
// the distinction impossible to drop by accident.
function recordAudit(record, summary, status = state.status) {
  const audit = backendAudit(status, summary);
  if (!audit) {
    return { evaluated: false, messages: [] };
  }
  const warningPayload = audit.warnings_by_record?.[record.record_id];
  if (Array.isArray(warningPayload) && warningPayload.length) {
    return {
      evaluated: true,
      messages: warningPayload
        .map((warning) => warning.message)
        .filter((message) => typeof message === 'string' && message),
    };
  }
  return { evaluated: true, messages: [] };
}

function renderScenarioWorkbench(status = state.status, events = state.events) {
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
      <div class="scenario-alert${warningCount ? ' has-warning' : ''}${readiness.pending ? ' is-pending' : ''}">
        <span>Audit readiness</span>
        <strong>${escapeHtml(readiness.pending ? (readiness.mismatched ? `Not scored for ${summary.label}` : 'Audit not evaluated yet') : warningCount ? `${warningCount} signal(s) still missing` : readiness.total > 0 ? 'Signals visible' : 'No checks evaluated')}</strong>
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

function renderStats(status) {
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

function deliveryTone(deliveryStatus) {
  if (deliveryStatus === 'posted') {
    return 'success';
  }
  if (deliveryStatus === 'failed') {
    return 'error';
  }
  return 'neutral';
}

function renderDeliverySummary(status) {
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

function deliveryRecoveryHint(events = []) {
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

function pickSpotlightRecord(events) {
  if (!events.length) {
    return null;
  }
  return (
    events.find((record) => record.event.cte_type === 'transformation') ||
    events.find((record) => record.event.cte_type === 'shipping') ||
    events[0]
  );
}

function spotlightFields(event) {
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

function renderRecordSpotlight(events) {
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
    ids.lotLookup.value = eventNode.currentTarget.dataset.spotlightLot;
    await lookupLineage();
  });
}

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function cteLabel(cteType) {
  return String(cteType || 'event').replaceAll('_', ' ');
}

function formatDateTime(value) {
  return escapeHtml(new Date(value).toLocaleString());
}

function formatKdeValue(value) {
  if (Array.isArray(value)) {
    return value.join(', ');
  }
  if (value && typeof value === 'object') {
    return JSON.stringify(value);
  }
  return value ?? '';
}

function renderEvents(events) {
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
      const audit = recordAudit(record, summary);
      const auditNotEvaluated = !audit.evaluated;
      const hasWarning = audit.messages.length > 0;
      return `
        <tr class="${hasWarning ? 'has-audit-warning' : auditNotEvaluated ? 'audit-not-evaluated' : ''}">
          <td>${record.sequence_no}</td>
          <td><span class="pill">${escapeHtml(event.cte_type)}</span></td>
          <td><button class="link-button" data-lot="${escapeHtml(event.traceability_lot_code)}">${escapeHtml(event.traceability_lot_code)}</button></td>
          <td>${escapeHtml(event.product_description)}</td>
          <td>${escapeHtml(event.location_name)}</td>
          <td>${escapeHtml(new Date(event.timestamp).toLocaleString())}</td>
          <td>${escapeHtml(record.destination_mode)}</td>
          <td>
            ${escapeHtml(record.delivery_attempts || 0)}
            ${hasWarning ? `<small class="status-warning">${escapeHtml(audit.messages[0])}</small>` : auditNotEvaluated ? '<small class="status-muted">Audit not evaluated</small>' : ''}
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
      ids.lotLookup.value = button.dataset.lot;
      await lookupLineage();
    });
  });
}

function renderLineage(payload, traceabilityLotCode) {
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
      const audit = recordAudit(record, scenarioSummary);
      const auditNotEvaluated = !audit.evaluated;
      const hasWarning = audit.messages.length > 0;
      const kdes = Object.entries(event.kdes || {})
        .slice(0, 6)
        .map(([key, value]) => `<li><strong>${escapeHtml(key)}:</strong> ${escapeHtml(formatKdeValue(value))}</li>`)
        .join('');
      return `
        <article class="lineage-card${hasWarning ? ' has-audit-warning' : auditNotEvaluated ? ' audit-not-evaluated' : ''}">
          <header>
            <h3>${escapeHtml(cteLabel(event.cte_type))}</h3>
            <span>${formatDateTime(event.timestamp)}</span>
          </header>
          <p><strong>Lot:</strong> ${escapeHtml(event.traceability_lot_code)}</p>
          <p><strong>Product:</strong> ${escapeHtml(event.product_description)}</p>
          <p><strong>Location:</strong> ${escapeHtml(event.location_name)}</p>
          ${hasWarning ? `<p class="lineage-warning">${escapeHtml(audit.messages.join(' • '))}</p>` : auditNotEvaluated ? '<p class="lineage-muted">Audit not evaluated for this scenario</p>' : ''}
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
      ids.lotLookup.value = button.dataset.lineageLot;
      await lookupLineage();
    });
  });
}

function renderImportResult(result) {
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

function renderSnapshot(status, events, health = state.health) {
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
    if (!status.running && status.last_error) {
      // The run loop died rather than being stopped. The API carries the
      // reason (#217); an idle-looking console that hid it would make the
      // operator hunt the server log for why their run ended.
      setStatus(`Production line stopped: ${status.last_error}`, 'error');
    } else {
      setStatus(status.running ? 'Production line is running.' : 'Production line is idle.');
    }
  }
}

async function refresh() {
  const [health, status, events] = await Promise.all([
    api('/api/health'),
    api('/api/simulate/status'),
    api('/api/events?limit=100'),
  ]);
  renderSnapshot(status, events.events, health);
}

function stopFallbackPolling() {
  if (state.fallbackTimer) {
    clearInterval(state.fallbackTimer);
    state.fallbackTimer = null;
  }
}

function startFallbackPolling() {
  if (state.fallbackTimer) {
    return;
  }
  state.fallbackTimer = setInterval(() => {
    refresh().catch((error) => {
      setStatus(error.message, 'error', 5000);
    });
  }, 2000);
}

function applyStreamSnapshot(payload) {
  if (!payload || !payload.status || !Array.isArray(payload.events)) {
    return;
  }
  renderSnapshot(payload.status, payload.events);
}

function connectLiveUpdates() {
  if (!('EventSource' in window)) {
    startFallbackPolling();
    return;
  }
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  state.eventSource = new EventSource('/api/simulate/stream?limit=100');

  state.eventSource.addEventListener('open', () => {
    state.reconnectDelayMs = 0;
    stopFallbackPolling();
  });

  state.eventSource.addEventListener('snapshot', (event) => {
    try {
      applyStreamSnapshot(JSON.parse(event.data));
    } catch (error) {
      setStatus(error.message, 'error', 5000);
    }
  });

  // On stream loss, poll for freshness right away and keep trying to get the
  // stream back with capped exponential backoff instead of giving up forever.
  state.eventSource.addEventListener('error', () => {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    startFallbackPolling();
    state.reconnectDelayMs = Math.min(Math.max(state.reconnectDelayMs || 0, 1000) * 2, 30000);
    if (!state.reconnectTimer) {
      state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        connectLiveUpdates();
      }, state.reconnectDelayMs);
    }
  });
}

const CONNECTION_VERDICT_TONES = {
  connected: 'success',
  mock: 'success',
  contract_mismatch: 'error',
  signature_mismatch: 'error',
  unauthorized: 'error',
  subscription_inactive: 'error',
  forbidden: 'error',
  tenant_mismatch: 'error',
  rate_limited: 'error',
  service_unavailable: 'error',
  unreachable: 'error',
  not_configured: 'neutral',
};

function renderConnectionStatus(integration) {
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

async function loadIntegrationStatus() {
  const integration = await api('/api/integration/status');
  renderConnectionStatus(integration);
}

async function saveIntegrationSettings() {
  try {
    const request = {
      mode: ids.deliveryMode.value,
      mock_friction: frictionSelections(),
    };
    const endpoint = ids.endpoint.value.trim();
    if (endpoint) {
      request.endpoint = endpoint;
    }
    const apiKey = ids.apiKey.value.trim();
    if (apiKey) {
      request.api_key = apiKey;
    }
    const tenantId = ids.tenantId.value.trim();
    if (tenantId) {
      request.tenant_id = tenantId;
    }
    const integration = await api('/api/integration/configure', {
      method: 'POST',
      body: JSON.stringify(request),
    });
    renderConnectionStatus(integration);
    setStatus('Saved RegEngine connection settings.', 'success', 2500);
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function testConnection() {
  if (ids.connectionResult) {
    ids.connectionResult.innerHTML = '<p class="note">Testing connection…</p>';
  }
  try {
    const request = {};
    const endpoint = ids.endpoint.value.trim();
    if (endpoint) {
      request.endpoint = endpoint;
    }
    const apiKey = ids.apiKey.value.trim();
    if (apiKey) {
      request.api_key = apiKey;
    }
    const tenantId = ids.tenantId.value.trim();
    if (tenantId) {
      request.tenant_id = tenantId;
    }
    const result = await api('/api/integration/test', {
      method: 'POST',
      body: JSON.stringify(request),
    });
    const tone = CONNECTION_VERDICT_TONES[result.verdict] || 'neutral';
    const statusLine = result.status_code ? ` (HTTP ${result.status_code})` : '';
    if (ids.connectionResult) {
      ids.connectionResult.innerHTML = `
        <div class="connection-verdict" data-tone="${escapeHtml(tone)}">
          <strong>${escapeHtml(result.verdict.replaceAll('_', ' '))}${escapeHtml(statusLine)}</strong>
          <p>${escapeHtml(result.detail)}</p>
        </div>
      `;
    }
    setStatus(`Connection test: ${result.verdict.replaceAll('_', ' ')}.`, tone === 'error' ? 'error' : 'success', 4000);
  } catch (error) {
    if (ids.connectionResult) {
      ids.connectionResult.innerHTML = `<div class="connection-verdict" data-tone="error"><p>${escapeHtml(error.message)}</p></div>`;
    }
    setStatus(error.message, 'error', 5000);
  }
}

async function startLoop() {
  try {
    await api('/api/simulate/start', {
      method: 'POST',
      body: JSON.stringify({ config: buildConfig() }),
    });
    setStatus('Started production line.', 'success', 2500);
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function stopLoop() {
  try {
    await api('/api/simulate/stop', { method: 'POST' });
    setStatus('Paused production line.', 'success', 2500);
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function stepOnce() {
  try {
    const result = await api('/api/simulate/step', { method: 'POST' });
    const traceLot = preferredTraceLot(result.lot_codes || []);
    if (traceLot) {
      ids.lotLookup.value = traceLot;
      ids.exportLot.value = traceLot;
      updateExportLink();
    }
    if (result.delivery_status === 'failed') {
      setStatus(`Recorded ${result.generated} event(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
    } else if (result.delivery_status === 'generated') {
      setStatus(`Recorded ${result.generated} event(s) without delivery.`, 'success', 2500);
    } else if (result.rejected > 0) {
      setStatus(`Recorded ${result.generated} event(s); RegEngine accepted ${result.accepted} and rejected ${result.rejected}.`, 'error', 7000);
    } else {
      setStatus(`Recorded and posted ${result.posted} event(s).`, 'success', 2500);
    }
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function retryFailedDeliveries() {
  try {
    const config = buildConfig();
    const result = await api('/api/delivery/retry', {
      method: 'POST',
      body: JSON.stringify({ delivery: config.delivery, source: config.source }),
    });
    if (result.status === 'empty') {
      setStatus('No failed deliveries are waiting to retry.', 'success', 2500);
    } else if (result.status === 'skipped') {
      setStatus(result.error || 'Retry skipped.', 'error', 5000);
    } else if (result.failed > 0) {
      setStatus(`Retried ${result.attempted} record(s): ${result.posted} posted, ${result.failed} failed.`, 'error', 7000);
    } else {
      setStatus(`Retried and posted ${result.posted} failed delivery record(s).`, 'success', 3500);
    }
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function saveCurrentScenario() {
  try {
    const config = buildConfig();
    const result = await api(`/api/scenario-saves/${encodeURIComponent(config.scenario)}`, {
      method: 'POST',
      body: JSON.stringify({ config }),
    });
    await loadScenarioSaves();
    ids.scenarioSave.value = result.save.scenario;
    updateScenarioSaveDescription();
    setStatus(`Saved ${result.save.label} with ${result.save.record_count} event(s).`, 'success', 3500);
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function loadSavedScenario() {
  const scenarioId = ids.scenarioSave.value;
  if (!scenarioId) {
    setStatus('Save a scenario first.', 'error', 5000);
    return;
  }
  try {
    const result = await api(`/api/scenario-saves/${encodeURIComponent(scenarioId)}/load`, {
      method: 'POST',
    });
    applyConfigToForm(result.config);
    ids.lineageResults.innerHTML = '';
    ids.importResults.innerHTML = '';
    await refresh();
    await loadScenarioSaves();
    ids.scenarioSave.value = result.save.scenario;
    updateScenarioSaveDescription();
    setStatus(`Loaded ${result.save.label} with ${result.loaded_records} saved event(s).`, 'success', 3500);
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function loadSelectedDemoFixture() {
  try {
    const config = buildConfig();
    const fixtureId = ids.demoFixture.value || 'leafy_greens_trace';
    const result = await api(`/api/demo-fixtures/${encodeURIComponent(fixtureId)}/load`, {
      method: 'POST',
      body: JSON.stringify({
        reset: true,
        source: config.source,
        delivery: config.delivery,
      }),
    });
    const fixtureScenario = state.scenarioCatalog[result.scenario];
    renderScenarioOptions(
      state.allScenarios.length ? state.allScenarios : Object.values(state.scenarioCatalog),
      result.scenario,
      fixtureScenario?.operation_type || null,
    );
    ids.lineageResults.innerHTML = '';
    const traceLot = preferredTraceLot(result.lot_codes || []);
    if (traceLot) {
      ids.lotLookup.value = traceLot;
      ids.exportLot.value = traceLot;
      updateExportLink();
    }
    if (result.status === 'delivery_failed') {
      setStatus(`Loaded ${result.stored} line event(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
    } else if (result.delivery_mode === 'none') {
      setStatus(`Loaded ${result.stored} line event(s) without delivery.`, 'success', 3500);
    } else {
      setStatus(`Loaded line data and posted ${result.posted} event(s).`, 'success', 3500);
    }
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function replayCurrentLog() {
  try {
    const result = await api('/api/simulate/replay', { method: 'POST' });
    const path = result.persist_path || 'current log';
    if (result.status === 'empty') {
      setStatus(`No persisted events found at ${path}.`, 'error', 5000);
    } else if (result.status === 'failed') {
      setStatus(`Replay failed for ${result.failed} event(s): ${result.error || 'delivery error'}`, 'error', 5000);
    } else if (result.status === 'rebuilt') {
      setStatus(`Rebuilt ${result.replayed} event(s) from ${path}; delivery mode is none.`, 'success', 3500);
    } else {
      setStatus(`Replayed ${result.posted} event(s) from ${path}.`, 'success', 3500);
    }
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function importCsv() {
  const file = ids.csvFile.files[0];
  if (!file) {
    setStatus('Choose a CSV file first.', 'error', 5000);
    return;
  }
  try {
    const config = buildConfig();
    const result = await api('/api/import/csv', {
      method: 'POST',
      body: JSON.stringify({
        import_type: ids.csvImportType.value,
        csv_text: await file.text(),
        source: config.source,
        delivery: config.delivery,
      }),
    });
    renderImportResult(result);
    if (result.status === 'delivery_failed') {
      setStatus(`Imported ${result.accepted} row(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
    } else if (result.rejected > 0) {
      setStatus(`Imported ${result.accepted} row(s); rejected ${result.rejected}.`, 'error', 7000);
    } else if ((result.warnings || []).length > 0) {
      setStatus(`Imported ${result.accepted} CSV row(s) with ${result.warnings.length} warning(s).`, 'success', 4500);
    } else {
      setStatus(`Imported ${result.accepted} CSV row(s).`, 'success', 3500);
    }
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function resetState() {
  try {
    await api('/api/simulate/reset', {
      method: 'POST',
      body: JSON.stringify(buildConfig()),
    });
    ids.lineageResults.innerHTML = '';
    setStatus('Cleared line state and shift log.', 'success', 2500);
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function lookupLineage() {
  const lotCode = ids.lotLookup.value.trim();
  if (!lotCode) {
    setStatus('Enter a lot code first.', 'error', 5000);
    return;
  }
  try {
    const payload = await api(`/api/lineage/${encodeURIComponent(lotCode)}`);
    renderLineage(payload, lotCode);
    journey.traced = true;
    renderGuide();
    setStatus(`Loaded lineage for ${lotCode}.`, 'success', 2500);
  } catch (error) {
    ids.lineageResults.innerHTML = `<p class="note">${escapeHtml(error.message)}</p>`;
    setStatus(error.message, 'error', 5000);
  }
}

bindAsyncClick(ids.startBtn, startLoop);
bindAsyncClick(ids.stopBtn, stopLoop);
bindAsyncClick(ids.stepBtn, stepOnce);
bindAsyncClick(ids.replayBtn, replayCurrentLog);
bindAsyncClick(ids.importCsvBtn, importCsv);
bindAsyncClick(ids.retryFailedBtn, retryFailedDeliveries);
bindAsyncClick(ids.saveScenarioBtn, saveCurrentScenario);
bindAsyncClick(ids.loadScenarioBtn, loadSavedScenario);
bindAsyncClick(ids.loadFixtureBtn, loadSelectedDemoFixture);
bindAsyncClick(ids.refreshBtn, refresh);
bindAsyncClick(ids.lineageBtn, lookupLineage);
bindAsyncClick(ids.testConnectionBtn, testConnection);
bindAsyncClick(ids.saveIntegrationBtn, saveIntegrationSettings);

// Clear shift is destructive, so the first click arms the button and only a
// second click within the arm window actually wipes the workspace.
let resetArmTimer = null;
function disarmReset() {
  if (resetArmTimer) {
    clearTimeout(resetArmTimer);
    resetArmTimer = null;
  }
  ids.resetBtn.classList.remove('is-armed');
  ids.resetBtn.textContent = 'Clear shift';
}
ids.resetBtn.addEventListener('click', () => {
  if (ids.resetBtn.dataset.busy === '1') {
    return;
  }
  if (!ids.resetBtn.classList.contains('is-armed')) {
    ids.resetBtn.classList.add('is-armed');
    ids.resetBtn.textContent = 'Confirm clear shift';
    setStatus('Clearing wipes every record in this workspace. Click Confirm clear shift to proceed.', 'neutral', 6000);
    resetArmTimer = setTimeout(disarmReset, 6000);
    return;
  }
  disarmReset();
  runWithBusy(ids.resetBtn, resetState);
});

// The setup grid is a <form>; without this, pressing Enter in a text field
// triggers the implicit submission and reloads the whole page.
document.getElementById('config-form').addEventListener('submit', (event) => event.preventDefault());

ids.lotLookup.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    runWithBusy(ids.lineageBtn, lookupLineage);
  }
});

document.getElementById('nextActionGo')?.addEventListener('click', goToNextAction);
for (const link of [ids.exportDownloadLink, ids.epcisDownloadLink]) {
  link?.addEventListener('click', () => {
    if (!state.events.length) {
      return;
    }
    journey.exported = true;
    renderGuide();
  });
}
document.querySelectorAll('#guideRail [data-guide-target]').forEach((step) => {
  step.addEventListener('click', () => flashPanel(step.dataset.guideTarget));
  step.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      flashPanel(step.dataset.guideTarget);
    }
  });
});

document.getElementById('tourBtn')?.addEventListener('click', startTour);
tourEls.next.addEventListener('click', () => advanceTour(1));
tourEls.back.addEventListener('click', () => advanceTour(-1));
tourEls.skip.addEventListener('click', endTour);
welcomeEls.tour.addEventListener('click', () => {
  hideWelcome();
  startTour();
});
welcomeEls.sample.addEventListener('click', () => {
  hideWelcome();
  runWithBusy(ids.loadFixtureBtn, loadSelectedDemoFixture);
  flashPanel('.run-panel');
});
welcomeEls.skip.addEventListener('click', hideWelcome);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') {
    return;
  }
  if (!welcomeEls.overlay.hidden) {
    hideWelcome();
  } else if (tour.active) {
    endTour();
  }
});
ids.scenarioSave.addEventListener('change', updateScenarioSaveDescription);
ids.demoFixture.addEventListener('change', updateDemoFixtureDescription);
ids.exportPreset.addEventListener('change', updateExportLink);
ids.exportLot.addEventListener('input', updateExportLink);
ids.exportStartDate.addEventListener('change', updateExportLink);
ids.exportEndDate.addEventListener('change', updateExportLink);
ids.deliveryMode.addEventListener('change', () => updateShellStatus());
ids.operationType.addEventListener('change', () => {
  renderScenarioOptions(state.allScenarios, ids.scenario.value, ids.operationType.value);
});
ids.scenario.addEventListener('change', () => {
  renderReadinessBanner(activeScenarioSummary(), state.events, state.status);
  renderScenarioWorkbench(state.status, state.events);
  renderRecordSpotlight(state.events);
  renderEvents(state.events);
});

loadScenarios().catch((error) => {
  setStatus(error.message, 'error', 5000);
});
loadScenarioSaves().catch((error) => {
  setStatus(error.message, 'error', 5000);
});
loadDemoFixtures().catch((error) => {
  setStatus(error.message, 'error', 5000);
});
loadExportPresets().catch((error) => {
  setStatus(error.message, 'error', 5000);
});
loadIntegrationStatus().catch((error) => {
  setStatus(error.message, 'error', 5000);
});
connectLiveUpdates();
refresh().catch((error) => {
  setStatus(error.message, 'error', 5000);
  startFallbackPolling();
});
maybeShowWelcome();

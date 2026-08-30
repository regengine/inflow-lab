// The DOM handles the console owns, and the plumbing that touches them.
//
// `ids` is resolved once at load, so every other module refers to elements
// through it rather than querying the document again.

import { state } from './state.js';

export const ids = {
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

export const tourEls = {
  popover: document.getElementById('tourPopover'),
  progress: document.getElementById('tourProgress'),
  title: document.getElementById('tourTitle'),
  body: document.getElementById('tourBody'),
  back: document.getElementById('tourBackBtn'),
  next: document.getElementById('tourNextBtn'),
  skip: document.getElementById('tourSkipBtn'),
};

export const welcomeEls = {
  overlay: document.getElementById('welcomeOverlay'),
  tour: document.getElementById('welcomeTourBtn'),
  sample: document.getElementById('welcomeSampleBtn'),
  skip: document.getElementById('welcomeSkipBtn'),
};

export function setStatus(message, tone = 'neutral', holdMs = 0) {
  ids.statusMessage.textContent = message;
  ids.statusMessage.dataset.tone = tone;
  state.statusHoldUntil = holdMs > 0 ? Date.now() + holdMs : 0;
}

// Keep Start/Pause honest about the loop state. Buttons mid-request keep
// their busy-disabled state until the request settles.
export function syncRunButtons(running = Boolean(state.status?.running)) {
  if (ids.startBtn.dataset.busy !== '1') {
    ids.startBtn.disabled = running;
  }
  if (ids.stopBtn.dataset.busy !== '1') {
    ids.stopBtn.disabled = !running;
  }
}

// Runs an async handler with the button disabled and spinning so double-clicks
// can't fire duplicate requests.
export async function runWithBusy(button, handler) {
  if (button.dataset.busy === '1') {
    return;
  }
  button.dataset.busy = '1';
  button.classList.add('is-busy');
  button.disabled = true;
  try {
    await handler();
  } catch (error) {
    // Most handlers report their own failures and never reach here. `refresh`
    // deliberately does not, so without this the Refresh button would spin and
    // then go silent while the rejection escaped the click handler entirely.
    setStatus(error.message, 'error', 5000);
  } finally {
    delete button.dataset.busy;
    button.classList.remove('is-busy');
    button.disabled = false;
    syncRunButtons();
  }
}

export function bindAsyncClick(button, handler) {
  button?.addEventListener('click', () => runWithBusy(button, handler));
}

export function flashPanel(selector) {
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

// Rendered markup wires lot-code buttons to a trace lookup, but the lookup is
// an action, and the actions already import the renderers. Importing back would
// make the two modules mutually dependent, so main.js registers the handler
// here and the renderers only ever depend on this one indirection.
let lotTraceHandler = async () => {};

export function onTraceLot(handler) {
  lotTraceHandler = handler;
}

export async function traceLotCode(lotCode) {
  ids.lotLookup.value = lotCode;
  await lotTraceHandler();
}

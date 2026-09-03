// Application state and the constant tables that describe it.
//
// Imports nothing: this is the bottom of the module graph.

export const state = {
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

// Progress through the guided flow, used to light up the "How to use this
// console" stepper. traced/exported are set by user actions; the rest are
// derived from live state on every snapshot.
export const journey = { traced: false, exported: false };

export const DEFAULT_LIVE_INGEST_ENDPOINT = 'https://www.regengine.co/api/v1/webhooks/ingest';

export const DELIVERY_MODE_LABELS = {
  mock: 'sandbox',
  live: 'connected',
  none: 'off',
};

// Recovery guidance keyed by the HTTP status a delivery failure carried —
// the same failure vocabulary a live RegEngine integration produces.
export const DELIVERY_RECOVERY_HINTS = {
  401: 'RegEngine rejected the API key. Fix the key in RegEngine connection settings, then retry.',
  402: 'The RegEngine subscription for this tenant is inactive. Reactivate billing, then retry — the retry reuses the original idempotency key, so nothing double-ingests.',
  403: 'RegEngine refused the request. Check that the tenant ID matches the API key and the key has the webhooks.ingest scope.',
  422: 'RegEngine rejected the request shape (for example, more than 500 events in one batch).',
  429: 'RegEngine is rate limiting this tenant. Wait for the window to pass, then retry.',
};

// Where "Take me there" should send the user for each next-action label.
export const NEXT_ACTION_TARGETS = {
  'Verify RegEngine connection': '.integration-panel',
  'Retry failed deliveries': '.delivery-monitor',
  'Load line data': '.run-panel',
  'Trace a lot': '.lineage-panel',
  'Export evidence': '.evidence-panel',
};

export const CONNECTION_VERDICT_TONES = {
  connected: 'success',
  mock: 'success',
  contract_mismatch: 'error',
  signature_mismatch: 'error',
  unauthorized: 'error',
  forbidden: 'error',
  tenant_mismatch: 'error',
  rate_limited: 'error',
  service_unavailable: 'error',
  unreachable: 'error',
  not_configured: 'neutral',
};

// Fuller copy than the guide rail's one-liners: each tour stop explains what
// the panel is for and what to actually click.
export const TOUR_STEPS = [
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

// --- Onboarding: first-run welcome + guided tour -------------------------
// Seen-state lives in localStorage so the welcome only interrupts once per
// browser; private-mode storage failures degrade to "never show".
export const ONBOARDING_KEY = 'inflowLab.onboarded.v1';

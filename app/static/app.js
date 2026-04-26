const state = {
  status: null,
  events: [],
  eventSource: null,
  fallbackTimer: null,
  statusHoldUntil: 0,
  scenarioLabels: {
    leafy_greens_supplier: 'Leafy greens supplier',
    fresh_cut_processor: 'Fresh-cut processor',
    retailer_readiness_demo: 'Retailer readiness demo',
  },
  demoFixtureDescriptions: {
    leafy_greens_trace: 'Harvest through cooling, packout, shipment, and DC receipt for one leafy greens lot.',
  },
  scenarioSaves: [],
  exportPresetDescriptions: {
    all_records: 'Full FDA-request export for the selected date range.',
  },
};

const ids = {
  source: document.getElementById('source'),
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
  exportPreset: document.getElementById('exportPreset'),
  exportLot: document.getElementById('exportLot'),
  exportStartDate: document.getElementById('exportStartDate'),
  exportEndDate: document.getElementById('exportEndDate'),
  exportDownloadLink: document.getElementById('exportDownloadLink'),
  exportPresetDescription: document.getElementById('exportPresetDescription'),
  statusMessage: document.getElementById('statusMessage'),
  statsGrid: document.getElementById('statsGrid'),
  deliverySummary: document.getElementById('deliverySummary'),
  retryFailedBtn: document.getElementById('retryFailedBtn'),
  eventsBody: document.getElementById('eventsBody'),
  lotLookup: document.getElementById('lotLookup'),
  lineageResults: document.getElementById('lineageResults'),
};

function setStatus(message, tone = 'neutral', holdMs = 0) {
  ids.statusMessage.textContent = message;
  ids.statusMessage.dataset.tone = tone;
  state.statusHoldUntil = holdMs > 0 ? Date.now() + holdMs : 0;
}

function buildConfig() {
  const endpoint = ids.endpoint.value.trim();
  const apiKey = ids.apiKey.value.trim();
  const tenantId = ids.tenantId.value.trim();
  const seedValue = ids.seed.value.trim();
  return {
    source: ids.source.value.trim() || 'codex-simulator',
    scenario: ids.scenario.value,
    interval_seconds: Number(ids.interval.value || 1.5),
    batch_size: Number(ids.batchSize.value || 3),
    seed: seedValue === '' ? null : Number(seedValue),
    persist_path: 'data/events.jsonl',
    delivery: {
      mode: ids.deliveryMode.value,
      endpoint: endpoint || null,
      api_key: apiKey || null,
      tenant_id: tenantId || null,
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

function renderScenarioOptions(scenarios) {
  const selected = ids.scenario.value || 'leafy_greens_supplier';
  state.scenarioLabels = Object.fromEntries(scenarios.map((scenario) => [scenario.id, scenario.label]));
  ids.scenario.innerHTML = scenarios
    .map(
      (scenario) => `
        <option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.label)}</option>
      `,
    )
    .join('');
  ids.scenario.value = state.scenarioLabels[selected] ? selected : scenarios[0]?.id || 'leafy_greens_supplier';
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
  ids.scenario.value = config.scenario || 'leafy_greens_supplier';
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
  const params = new URLSearchParams();
  const preset = ids.exportPreset.value || 'all_records';
  const lotCode = ids.exportLot.value.trim();
  const startDate = ids.exportStartDate.value;
  const endDate = ids.exportEndDate.value;
  params.set('preset', preset);
  if (lotCode) {
    params.set('traceability_lot_code', lotCode);
  }
  if (startDate) {
    params.set('start_date', startDate);
  }
  if (endDate) {
    params.set('end_date', endDate);
  }
  ids.exportDownloadLink.href = `/api/mock/regengine/export/fda-request?${params.toString()}`;
  ids.exportPresetDescription.textContent = state.exportPresetDescriptions[preset] || 'FDA-request CSV export.';
}

function renderStats(status) {
  const stats = status?.stats || {};
  const engine = stats.engine || {};
  const scenarioId = status?.config?.scenario || ids.scenario.value;
  const cards = [
    ['Loop status', status?.running ? 'Running' : 'Stopped'],
    ['Scenario', scenarioLabel(scenarioId)],
    ['Total records', stats.total_records ?? 0],
    ['Unique lots', stats.unique_lots ?? 0],
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
          <span>${label}</span>
          <strong>${value}</strong>
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
  ids.deliverySummary.innerHTML = `
    <div class="delivery-cards">
      ${cards
        .map(
          ([label, value, tone]) => `
            <article class="delivery-card" data-tone="${tone}">
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
    </dl>
  `;
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
  if (!events.length) {
    ids.eventsBody.innerHTML = `
      <tr>
        <td colspan="9" class="empty-state">No events yet.</td>
      </tr>
    `;
    return;
  }
  ids.eventsBody.innerHTML = events
    .map((record) => {
      const event = record.event;
      return `
        <tr>
          <td>${record.sequence_no}</td>
          <td><span class="pill">${escapeHtml(event.cte_type)}</span></td>
          <td><button class="link-button" data-lot="${escapeHtml(event.traceability_lot_code)}">${escapeHtml(event.traceability_lot_code)}</button></td>
          <td>${escapeHtml(event.product_description)}</td>
          <td>${escapeHtml(event.location_name)}</td>
          <td>${escapeHtml(new Date(event.timestamp).toLocaleString())}</td>
          <td>${escapeHtml(record.destination_mode)}</td>
          <td>${escapeHtml(record.delivery_attempts || 0)}</td>
          <td>
            <span class="status-pill" data-tone="${deliveryTone(record.delivery_status)}">${escapeHtml(record.delivery_status)}</span>
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

  const summary = queriedNode
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
      const kdes = Object.entries(event.kdes || {})
        .slice(0, 6)
        .map(([key, value]) => `<li><strong>${escapeHtml(key)}:</strong> ${escapeHtml(formatKdeValue(value))}</li>`)
        .join('');
      return `
        <article class="lineage-card">
          <header>
            <h3>${escapeHtml(cteLabel(event.cte_type))}</h3>
            <span>${formatDateTime(event.timestamp)}</span>
          </header>
          <p><strong>Lot:</strong> ${escapeHtml(event.traceability_lot_code)}</p>
          <p><strong>Product:</strong> ${escapeHtml(event.product_description)}</p>
          <p><strong>Location:</strong> ${escapeHtml(event.location_name)}</p>
          <ul>${kdes}</ul>
        </article>
      `;
    })
    .join('');

  ids.lineageResults.innerHTML = `
    <div class="lineage-overview">
      ${summary}
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
  const errorList = errors
    .map((error) => {
      const field = error.field ? ` ${escapeHtml(error.field)}:` : '';
      return `<li>Row ${escapeHtml(error.row)}${field} ${escapeHtml(error.message)}</li>`;
    })
    .join('');
  ids.importResults.innerHTML = `
    <div class="import-summary" data-tone="${tone}">
      Accepted ${escapeHtml(result.accepted)} of ${escapeHtml(result.total)} row(s).
      Stored ${escapeHtml(result.stored)}; posted ${escapeHtml(result.posted)}; rejected ${escapeHtml(result.rejected)}.
      ${result.error ? `<span>${escapeHtml(result.error)}</span>` : ''}
    </div>
    ${errorList ? `<ul>${errorList}</ul>` : ''}
  `;
}

function renderSnapshot(status, events) {
  state.status = status;
  state.events = events;
  renderStats(status);
  renderDeliverySummary(status);
  renderEvents(events);
  if (Date.now() >= state.statusHoldUntil) {
    setStatus(status.running ? 'Simulator loop is running.' : 'Simulator loop is stopped.');
  }
}

async function refresh() {
  const [status, events] = await Promise.all([api('/api/simulate/status'), api('/api/events?limit=100')]);
  renderSnapshot(status, events.events);
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

  state.eventSource = new EventSource('/api/simulate/stream?limit=100');

  state.eventSource.addEventListener('open', () => {
    stopFallbackPolling();
  });

  state.eventSource.addEventListener('snapshot', (event) => {
    try {
      applyStreamSnapshot(JSON.parse(event.data));
    } catch (error) {
      setStatus(error.message, 'error', 5000);
    }
  });

  state.eventSource.addEventListener('error', () => {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    setStatus('Live update stream disconnected. Falling back to refresh.', 'error', 5000);
    startFallbackPolling();
  });
}

async function startLoop() {
  try {
    await api('/api/simulate/start', {
      method: 'POST',
      body: JSON.stringify({ config: buildConfig() }),
    });
    setStatus('Started simulator loop.', 'success', 2500);
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function stopLoop() {
  try {
    await api('/api/simulate/stop', { method: 'POST' });
    setStatus('Stopped simulator loop.', 'success', 2500);
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error', 5000);
  }
}

async function stepOnce() {
  try {
    const result = await api('/api/simulate/step', { method: 'POST' });
    if (result.delivery_status === 'failed') {
      setStatus(`Generated ${result.generated} event(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
    } else if (result.delivery_status === 'generated') {
      setStatus(`Generated ${result.generated} event(s) without delivery.`, 'success', 2500);
    } else {
      setStatus(`Generated and posted ${result.posted} event(s).`, 'success', 2500);
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
    ids.scenario.value = result.scenario;
    ids.lineageResults.innerHTML = '';
    if (result.status === 'delivery_failed') {
      setStatus(`Loaded ${result.stored} fixture event(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
    } else if (result.delivery_mode === 'none') {
      setStatus(`Loaded ${result.stored} fixture event(s) without delivery.`, 'success', 3500);
    } else {
      setStatus(`Loaded fixture and posted ${result.posted} event(s).`, 'success', 3500);
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
    setStatus('Reset simulator state and persisted event log.', 'success', 2500);
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
    setStatus(`Loaded lineage for ${lotCode}.`, 'success', 2500);
  } catch (error) {
    ids.lineageResults.innerHTML = `<p class="note">${escapeHtml(error.message)}</p>`;
    setStatus(error.message, 'error', 5000);
  }
}

document.getElementById('startBtn').addEventListener('click', startLoop);
document.getElementById('stopBtn').addEventListener('click', stopLoop);
document.getElementById('stepBtn').addEventListener('click', stepOnce);
document.getElementById('replayBtn').addEventListener('click', replayCurrentLog);
document.getElementById('importCsvBtn').addEventListener('click', importCsv);
document.getElementById('retryFailedBtn').addEventListener('click', retryFailedDeliveries);
document.getElementById('saveScenarioBtn').addEventListener('click', saveCurrentScenario);
document.getElementById('loadScenarioBtn').addEventListener('click', loadSavedScenario);
document.getElementById('loadFixtureBtn').addEventListener('click', loadSelectedDemoFixture);
document.getElementById('resetBtn').addEventListener('click', resetState);
document.getElementById('refreshBtn').addEventListener('click', refresh);
document.getElementById('lineageBtn').addEventListener('click', lookupLineage);
ids.scenarioSave.addEventListener('change', updateScenarioSaveDescription);
ids.demoFixture.addEventListener('change', updateDemoFixtureDescription);
ids.exportPreset.addEventListener('change', updateExportLink);
ids.exportLot.addEventListener('input', updateExportLink);
ids.exportStartDate.addEventListener('change', updateExportLink);
ids.exportEndDate.addEventListener('change', updateExportLink);

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
connectLiveUpdates();
refresh().catch((error) => {
  setStatus(error.message, 'error', 5000);
  startFallbackPolling();
});

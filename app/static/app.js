const state = {
  status: null,
  events: [],
};

const DEFAULT_LIVE_INGEST_ENDPOINT = 'https://www.regengine.co/api/v1/webhooks/ingest';

const ids = {
  source: document.getElementById('source'),
  interval: document.getElementById('interval'),
  batchSize: document.getElementById('batchSize'),
  seed: document.getElementById('seed'),
  deliveryMode: document.getElementById('deliveryMode'),
  endpoint: document.getElementById('endpoint'),
  apiKey: document.getElementById('apiKey'),
  tenantId: document.getElementById('tenantId'),
  liveConfirmBlock: document.getElementById('liveConfirmBlock'),
  liveConfirmed: document.getElementById('liveConfirmed'),
  startBtn: document.getElementById('startBtn'),
  stepBtn: document.getElementById('stepBtn'),
  statusMessage: document.getElementById('statusMessage'),
  statsGrid: document.getElementById('statsGrid'),
  eventsBody: document.getElementById('eventsBody'),
  lotLookup: document.getElementById('lotLookup'),
  lineageResults: document.getElementById('lineageResults'),
};

function setStatus(message, tone = 'neutral') {
  ids.statusMessage.textContent = message;
  ids.statusMessage.dataset.tone = tone;
}

function buildConfig() {
  const endpoint = ids.endpoint.value.trim() || DEFAULT_LIVE_INGEST_ENDPOINT;
  const apiKey = ids.apiKey.value.trim();
  const tenantId = ids.tenantId.value.trim();
  const seedValue = ids.seed.value.trim();
  return {
    source: ids.source.value.trim() || 'codex-simulator',
    interval_seconds: Number(ids.interval.value || 1.5),
    batch_size: Number(ids.batchSize.value || 3),
    seed: seedValue === '' ? null : Number(seedValue),
    persist_path: 'data/events.jsonl',
    delivery: {
      mode: ids.deliveryMode.value,
      endpoint: endpoint || null,
      api_key: apiKey || null,
      tenant_id: tenantId || null,
      live_confirmed: ids.liveConfirmed.checked,
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

function renderStats(status) {
  const stats = status?.stats || {};
  const engine = stats.engine || {};
  const deliveryStatuses = stats.by_delivery_status || {};
  const cards = [
    ['Loop status', status?.running ? 'Running' : 'Stopped'],
    ['Total records', stats.total_records ?? 0],
    ['Unique lots', stats.unique_lots ?? 0],
    ['Accepted', deliveryStatuses.accepted ?? 0],
    ['Rejected', deliveryStatuses.rejected ?? 0],
    ['Failed', deliveryStatuses.failed ?? 0],
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

function responseMessages(payload) {
  if (!payload || typeof payload !== 'object') {
    return [];
  }
  const messages = [];
  ['detail', 'error', 'message'].forEach((key) => {
    const value = payload[key];
    if (typeof value === 'string' && value.trim()) {
      messages.push(value);
    }
  });
  const errors = payload.errors;
  if (Array.isArray(errors)) {
    errors.forEach((error) => messages.push(typeof error === 'string' ? error : JSON.stringify(error)));
  } else if (typeof errors === 'string' && errors.trim()) {
    messages.push(errors);
  }
  return messages;
}

function deliveryDetail(record) {
  const messages = [...responseMessages(record.delivery_response)];
  if (record.error) {
    messages.unshift(record.error);
  }
  return messages.filter(Boolean).slice(0, 3).join(' | ');
}

function stepSummary(result) {
  const pieces = [`Generated ${result.generated} event(s)`];
  if ((result.accepted ?? 0) > 0) {
    pieces.push(`${result.accepted} accepted`);
  }
  if ((result.rejected ?? 0) > 0) {
    pieces.push(`${result.rejected} rejected`);
  }
  if ((result.failed ?? 0) > 0) {
    pieces.push(`${result.failed} failed`);
  }
  const validationErrors = (result.response?.events || [])
    .flatMap((event) => responseMessages(event))
    .filter(Boolean);
  if (validationErrors.length) {
    pieces.push(validationErrors.slice(0, 2).join(' | '));
  }
  return pieces.join('. ') + '.';
}

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function renderEvents(events) {
  if (!events.length) {
    ids.eventsBody.innerHTML = `
      <tr>
        <td colspan="7" class="empty-state">No events yet.</td>
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
          <td>
            <span class="status-badge" data-status="${escapeHtml(record.delivery_status)}">${escapeHtml(record.delivery_status)}</span>
            ${deliveryDetail(record) ? `<div class="delivery-detail">${escapeHtml(deliveryDetail(record))}</div>` : ''}
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

function renderLineage(records, traceabilityLotCode) {
  if (!records.length) {
    ids.lineageResults.innerHTML = `<p class="note">No lineage found for ${escapeHtml(traceabilityLotCode)}.</p>`;
    return;
  }
  ids.lineageResults.innerHTML = records
    .map((record) => {
      const event = record.event;
      const kdes = Object.entries(event.kdes || {})
        .slice(0, 6)
        .map(([key, value]) => `<li><strong>${escapeHtml(key)}:</strong> ${escapeHtml(Array.isArray(value) ? value.join(', ') : value)}</li>`)
        .join('');
      return `
        <article class="lineage-card">
          <header>
            <h3>${escapeHtml(event.cte_type)}</h3>
            <span>${escapeHtml(new Date(event.timestamp).toLocaleString())}</span>
          </header>
          <p><strong>Lot:</strong> ${escapeHtml(event.traceability_lot_code)}</p>
          <p><strong>Product:</strong> ${escapeHtml(event.product_description)}</p>
          <p><strong>Location:</strong> ${escapeHtml(event.location_name)}</p>
          <ul>${kdes}</ul>
        </article>
      `;
    })
    .join('');
}

async function refresh() {
  const [status, events] = await Promise.all([api('/api/simulate/status'), api('/api/events?limit=100')]);
  state.status = status;
  state.events = events.events;
  renderStats(status);
  renderEvents(events.events);
  setStatus(status.running ? 'Simulator loop is running.' : 'Simulator loop is stopped.');
}

async function startLoop() {
  try {
    await api('/api/simulate/start', {
      method: 'POST',
      body: JSON.stringify({ config: buildConfig() }),
    });
    setStatus('Started simulator loop.', 'success');
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function stopLoop() {
  try {
    await api('/api/simulate/stop', { method: 'POST' });
    setStatus('Stopped simulator loop.', 'success');
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function stepOnce() {
  try {
    const result = await api('/api/simulate/step', {
      method: 'POST',
      body: JSON.stringify({ config: buildConfig() }),
    });
    const hasErrors = (result.rejected ?? 0) > 0 || (result.failed ?? 0) > 0;
    setStatus(stepSummary(result), hasErrors ? 'error' : 'success');
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function resetState() {
  try {
    await api('/api/simulate/reset', {
      method: 'POST',
      body: JSON.stringify(buildConfig()),
    });
    ids.lineageResults.innerHTML = '';
    setStatus('Reset simulator state and persisted event log.', 'success');
    await refresh();
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function lookupLineage() {
  const lotCode = ids.lotLookup.value.trim();
  if (!lotCode) {
    setStatus('Enter a lot code first.', 'error');
    return;
  }
  try {
    const payload = await api(`/api/lineage/${encodeURIComponent(lotCode)}`);
    renderLineage(payload.records, lotCode);
    setStatus(`Loaded lineage for ${lotCode}.`, 'success');
  } catch (error) {
    ids.lineageResults.innerHTML = `<p class="note">${escapeHtml(error.message)}</p>`;
    setStatus(error.message, 'error');
  }
}

document.getElementById('startBtn').addEventListener('click', startLoop);
document.getElementById('stopBtn').addEventListener('click', stopLoop);
document.getElementById('stepBtn').addEventListener('click', stepOnce);
document.getElementById('resetBtn').addEventListener('click', resetState);
document.getElementById('refreshBtn').addEventListener('click', refresh);
document.getElementById('lineageBtn').addEventListener('click', lookupLineage);
ids.deliveryMode.addEventListener('change', updateLiveControls);
ids.liveConfirmed.addEventListener('change', updateLiveControls);

function updateLiveControls() {
  const isLive = ids.deliveryMode.value === 'live';
  ids.liveConfirmBlock.hidden = !isLive;
  ids.endpoint.required = isLive;
  ids.apiKey.required = isLive;
  ids.tenantId.required = isLive;
  const liveBlocked = isLive && !ids.liveConfirmed.checked;
  ids.startBtn.disabled = liveBlocked;
  ids.stepBtn.disabled = liveBlocked;
}

setInterval(() => {
  refresh().catch((error) => {
    setStatus(error.message, 'error');
  });
}, 2000);

updateLiveControls();
refresh().catch((error) => {
  setStatus(error.message, 'error');
});

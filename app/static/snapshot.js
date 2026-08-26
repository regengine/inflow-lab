// One snapshot render, and the two ways a snapshot arrives: the SSE stream
// and the polling fallback. The sequence numbers and the revision guard here
// are what stop a slow response from repainting over newer state.

import { state } from './dom.js';
import { api } from './api.js';
import { setStatus } from './ui.js';
import { renderGuide } from './guide.js';
import { activeScenarioSummary, renderReadinessBanner, renderScenarioWorkbench } from './audit.js';
import { updateShellStatus, renderStats, renderDeliverySummary, renderRecordSpotlight, hydrateIntegrationStatus } from './panels.js';
import { renderEvents } from './shift-log.js';
import { hydrateDeliveryForm } from './config-form.js';

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

// Snapshot ordering: every render carries a sequence number so a slow
// response can never repaint over a newer one, and failures are reported
// instead of escaping as an unhandled rejection.
export let snapshotSeq = 0;

export async function refresh() {
  const requestSeq = ++snapshotSeq;
  try {
    const [health, status, events] = await Promise.all([
      api('/api/health'),
      api('/api/simulate/status'),
      api('/api/events?limit=100'),
    ]);
    if (requestSeq !== snapshotSeq) {
      return;
    }
    renderSnapshot(status, events.events, health);
    hydrateDeliveryForm(status);
    await hydrateIntegrationStatus();
    return true;
  } catch (error) {
    setStatus(error.message, 'error', 5000);
    return false;
  }
}

export function stopFallbackPolling() {
  if (state.fallbackTimer) {
    clearInterval(state.fallbackTimer);
    state.fallbackTimer = null;
  }
}

export function startFallbackPolling() {
  if (state.fallbackTimer) {
    return;
  }
  state.fallbackTimer = setInterval(() => {
    // refresh() reports its own failures; the in-flight guard keeps a slow
    // poll from stacking up behind itself.
    if (state.refreshInFlight) {
      return;
    }
    state.refreshInFlight = true;
    refresh().finally(() => {
      state.refreshInFlight = false;
    });
  }, 2000);
}

export function applyStreamSnapshot(payload) {
  if (!payload || !payload.status || !Array.isArray(payload.events)) {
    return;
  }
  // Stream snapshots carry the controller revision; an out-of-order or
  // replayed one must not repaint over newer state.
  const revision = Number(payload.revision);
  if (Number.isFinite(revision)) {
    if (revision < Number(state.lastRevision || 0)) {
      return;
    }
    state.lastRevision = revision;
  }
  snapshotSeq += 1;
  renderSnapshot(payload.status, payload.events);
}

export function connectLiveUpdates() {
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

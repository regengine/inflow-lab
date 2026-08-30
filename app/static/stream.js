// The SSE stream, its polling fallback, and the refresh they both drive.

import { state } from './state.js';
import { setStatus } from './dom.js';
import { api } from './api.js';
import { renderSnapshot } from './render.js';

export async function refresh() {
  const [health, status, events] = await Promise.all([
    api('/api/health'),
    api('/api/simulate/status'),
    api('/api/events?limit=100'),
  ]);
  renderSnapshot(status, events.events, health);
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
    refresh().catch((error) => {
      setStatus(error.message, 'error', 5000);
    });
  }, 2000);
}

export function applyStreamSnapshot(payload) {
  if (!payload || !payload.status || !Array.isArray(payload.events)) {
    return;
  }
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

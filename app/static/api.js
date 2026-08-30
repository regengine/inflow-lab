// The fetch wrapper, and the request bodies built from the setup form.

import { DEFAULT_LIVE_INGEST_ENDPOINT } from './state.js';
import { ids } from './dom.js';

export async function api(path, options = {}) {
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

export function buildConfig() {
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

export function frictionSelections() {
  return [ids.frictionInvalidKey, ids.frictionSubscription, ids.frictionRateLimit]
    .filter((checkbox) => checkbox && checkbox.checked)
    .map((checkbox) => checkbox.value);
}

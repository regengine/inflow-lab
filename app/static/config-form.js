// The delivery half of the setup form: what this tab has typed, what the
// server already holds, and the config payload every action posts.

import { ids, state } from './dom.js';
import { setStatus, flashPanel } from './ui.js';
import { updateShellStatus } from './panels.js';
import { DEFAULT_LIVE_INGEST_ENDPOINT } from './config.js';

export function frictionSelections() {
  return [ids.frictionInvalidKey, ids.frictionSubscription, ids.frictionRateLimit]
    .filter((checkbox) => checkbox && checkbox.checked)
    .map((checkbox) => checkbox.value);
}

// Which delivery fields the operator has edited in *this* tab. Server state
// hydrates only the ones they have not touched, so a background refresh can
// never stomp a field mid-edit.
export const deliveryFieldsTouched = { deliveryMode: false, endpoint: false, apiKey: false, tenantId: false };

export function hydrateField(field, value) {
  const node = ids[field];
  if (!node || deliveryFieldsTouched[field] || document.activeElement === node) {
    return;
  }
  if (node.value !== value) {
    node.value = value;
  }
}

// A plain reload (or a second tab) used to leave these fields at their HTML
// defaults — Sandbox, blank endpoint — while the server kept running live.
// buildConfig() then resent that stale form on the next Start/Reset/Retry and
// silently downgraded delivery. Hydrating from /api/simulate/status keeps the
// form and the server in agreement.
export function hydrateDeliveryForm(status = state.status) {
  const delivery = status?.config?.delivery;
  if (!delivery) {
    return;
  }
  state.serverDelivery = delivery;
  hydrateField('deliveryMode', delivery.mode || 'mock');
  hydrateField('endpoint', delivery.endpoint || '');
  // status scrubs tenant_id in live mode; only hydrate what the server sent.
  if (delivery.tenant_id) {
    hydrateField('tenantId', delivery.tenant_id);
  }
  updateShellStatus();
}

// The server holds credentials this tab was never given (status scrubs the
// API key, and the tenant ID in live mode). Posting the blank form would
// replace them with nothing, so a mutating action stops with an actionable
// message instead of silently clearing the connection.
export function blockedByMissingCredentials() {
  const integration = state.integration;
  if (!integration) {
    return false;
  }
  const missingKey = integration.api_key_configured && !ids.apiKey.value.trim();
  const missingTenant = integration.tenant_configured && !ids.tenantId.value.trim();
  if (!missingKey && !missingTenant) {
    return false;
  }
  const missing = [missingKey ? 'API key' : '', missingTenant ? 'tenant ID' : ''].filter(Boolean).join(' and ');
  setStatus(
    `This tab does not hold the saved RegEngine ${missing}. Re-enter it in RegEngine connection settings (Save settings) before running this action — submitting now would clear it.`,
    'error',
    9000,
  );
  flashPanel('.integration-panel');
  return true;
}

export function buildConfig() {
  // A blank endpoint used to mean the hard-coded production URL in *every*
  // mode. Only live delivery falls back to it now; sandbox and off send null.
  const endpoint =
    ids.endpoint.value.trim() || (ids.deliveryMode.value === 'live' ? DEFAULT_LIVE_INGEST_ENDPOINT : '');
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
    // Deliberately omitted: the server derives the default log path from
    // REGENGINE_DATA_DIR. Sending a literal here overrode that and put the
    // default scope's events outside a mounted volume.
    delivery: {
      mode: ids.deliveryMode.value,
      endpoint: endpoint || null,
      api_key: apiKey || null,
      tenant_id: tenantId || null,
      mock_friction: frictionSelections(),
    },
  };
}

for (const field of ['deliveryMode', 'endpoint', 'apiKey', 'tenantId']) {
  ids[field]?.addEventListener('input', () => {
    deliveryFieldsTouched[field] = true;
  });
  ids[field]?.addEventListener('change', () => {
    deliveryFieldsTouched[field] = true;
  });
}

// Operator-facing vocabulary: the words and tones the console shows for
// delivery modes, delivery failures, connection verdicts, and scenarios.

import { state } from './dom.js';

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

export const CONNECTION_VERDICT_TONES = {
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

export function deliveryTone(deliveryStatus) {
  if (deliveryStatus === 'posted') {
    return 'success';
  }
  if (deliveryStatus === 'failed') {
    return 'error';
  }
  return 'neutral';
}

export function scenarioLabel(scenarioId) {
  return state.scenarioLabels[scenarioId] || scenarioId || 'Unknown';
}

export function operationTypeLabel(operationType) {
  return state.operationTypeLabels[operationType] || operationType || 'Unknown';
}

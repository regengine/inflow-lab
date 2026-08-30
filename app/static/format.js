// Pure display helpers: no DOM and no fetch. Two of them read the label tables
// off `state`, which is the only dependency this module has.

import { state } from './state.js';

export function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function cteLabel(cteType) {
  return String(cteType || 'event').replaceAll('_', ' ');
}

export function formatDateTime(value) {
  return escapeHtml(new Date(value).toLocaleString());
}

export function formatKdeValue(value) {
  if (Array.isArray(value)) {
    return value.join(', ');
  }
  if (value && typeof value === 'object') {
    return JSON.stringify(value);
  }
  return value ?? '';
}

export function scenarioLabel(scenarioId) {
  return state.scenarioLabels[scenarioId] || scenarioId || 'Unknown';
}

export function operationTypeLabel(operationType) {
  return state.operationTypeLabels[operationType] || operationType || 'Unknown';
}

export function deliveryTone(deliveryStatus) {
  if (deliveryStatus === 'posted') {
    return 'success';
  }
  if (deliveryStatus === 'failed') {
    return 'error';
  }
  return 'neutral';
}

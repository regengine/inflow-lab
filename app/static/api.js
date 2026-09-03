<<<<<<< HEAD
// The fetch wrapper, and the request bodies built from the setup form.

import { DEFAULT_LIVE_INGEST_ENDPOINT } from './state.js';
import { ids } from './dom.js';
=======
// The fetch wrapper and the error-message flattening behind it. Nothing in
// here touches the DOM, so an error string is produced the same way for a
// button handler, a stream reconnect, or a background refresh.

// FastAPI answers a validation failure with `detail` as a *list of objects*.
// Interpolating that into an Error stringifies it to "[object Object]" and
// throws away the only actionable half of the response, so flatten it into
// "field: message" sentences first. String details pass through unchanged.
export function formatErrorDetail(detail) {
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') {
          return item;
        }
        if (!item || typeof item !== 'object') {
          return '';
        }
        const location = Array.isArray(item.loc)
          ? item.loc.filter((part) => part !== 'body').at(-1)
          : null;
        const message = typeof item.msg === 'string' ? item.msg : '';
        if (!message) {
          return '';
        }
        return location ? `${location}: ${message}` : message;
      })
      .filter(Boolean);
    if (messages.length) {
      return messages.join('; ');
    }
  }
  if (detail && typeof detail === 'object') {
    const message = typeof detail.msg === 'string' ? detail.msg : '';
    if (message) {
      return message;
    }
  }
  return '';
}

// Error bodies are not always JSON — a proxy 502 is usually an HTML page, and
// swallowing it left the operator with a bare status code. Fall back to the
// HTTP status plus a short snippet of whatever text came back.
export async function errorMessageFor(response) {
  const raw = await response.text().catch(() => '');
  let detail = '';
  if (raw) {
    try {
      detail = formatErrorDetail(JSON.parse(raw).detail);
    } catch (error) {
      detail = '';
    }
  }
  const statusLine = `Request failed: ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`;
  if (detail) {
    return `${statusLine} — ${detail}`;
  }
  const snippet = raw.replace(/\s+/g, ' ').trim().slice(0, 160);
  return snippet ? `${statusLine} — ${snippet}` : statusLine;
}
>>>>>>> origin/main

export async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
<<<<<<< HEAD
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed: ${response.status}`);
=======
    throw new Error(await errorMessageFor(response));
>>>>>>> origin/main
  }
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return response.json();
  }
  return response.text();
}
<<<<<<< HEAD

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
=======
>>>>>>> origin/main

// Backend-owned console defaults.
//
// The live RegEngine ingest endpoint has exactly one source of truth —
// DEFAULT_LIVE_INGEST_ENDPOINT in app/regengine_client.py — which reaches the
// console as `default_endpoint` on GET /api/integration/status. The console
// used to keep its own copy of that URL (issue #155) and buildConfig() sent it
// explicitly on every live submit, so the backend's own fallback was
// unreachable from here and a changed constant silently kept posting to the
// stale URL.
//
// Until the status response lands this stays empty, which buildConfig() sends
// as "no endpoint override" — exactly the case the backend answers with its
// own constant.
import { ids } from './dom.js';

export let DEFAULT_LIVE_INGEST_ENDPOINT = '';

export function adoptIntegrationDefaults(integration) {
  const value = integration?.default_endpoint;
  if (typeof value !== 'string' || !value || value === DEFAULT_LIVE_INGEST_ENDPOINT) {
    return;
  }
  DEFAULT_LIVE_INGEST_ENDPOINT = value;
  // The endpoint field's placeholder was a third copy of the same literal.
  if (ids.endpoint && !ids.endpoint.placeholder) {
    ids.endpoint.placeholder = value;
  }
}

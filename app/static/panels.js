// The read-only panels: header shell, run stats, delivery monitor, record
// spotlight, CSV import results, and the RegEngine connection chips.

import { ids, state } from './dom.js';
import { escapeHtml, cteLabel, formatDateTime, formatKdeValue } from './format.js';
import { DELIVERY_MODE_LABELS, DELIVERY_RECOVERY_HINTS, scenarioLabel } from './labels.js';
import { syncRunButtons } from './ui.js';
import { api } from './api.js';
import { adoptIntegrationDefaults } from './config.js';
import { lookupLineage } from './lineage.js';

export function nextAction(status, events, deliveryMode) {
  const delivery = status?.stats?.delivery || {};
  if (deliveryMode === 'live') {
    return 'Verify RegEngine connection';
  }
  if (Number(delivery.retryable || 0) > 0) {
    return 'Retry failed deliveries';
  }
  if (!events.length) {
    return 'Load line data';
  }
  if (!ids.lotLookup.value.trim()) {
    return 'Trace a lot';
  }
  return 'Export evidence';
}

export function updateShellStatus(status = state.status, events = state.events, health = state.health) {
  const deliveryMode = status?.config?.delivery?.mode || ids.deliveryMode.value || 'mock';
  const build = health?.build || {};
  ids.tenantBadge.textContent = health?.tenant || 'local-demo';
  ids.deliveryModePill.textContent = DELIVERY_MODE_LABELS[deliveryMode] || deliveryMode;
  ids.deliveryModePill.dataset.tone = deliveryMode === 'live' ? 'error' : deliveryMode === 'mock' ? 'success' : 'neutral';
  ids.runStatePill.textContent = status?.running ? 'Running' : 'Idle';
  ids.runStatePill.dataset.tone = status?.running ? 'success' : 'neutral';
  syncRunButtons(Boolean(status?.running));
  ids.buildBadge.textContent = build.commit_sha_short ? `${build.version || '0.1.0'} ${build.commit_sha_short}` : build.version || '0.1.0';
  ids.liveDeliveryWarning.hidden = deliveryMode !== 'live';
  ids.nextActionText.textContent = nextAction(status, events || [], deliveryMode);
  if (ids.connectionPill) {
    ids.connectionPill.textContent =
      deliveryMode === 'live' ? 'Connected' : deliveryMode === 'mock' ? 'Sandbox' : 'Off';
    ids.connectionPill.dataset.tone = deliveryMode === 'live' ? 'error' : deliveryMode === 'mock' ? 'success' : 'neutral';
  }
}

export function renderStats(status) {
  const stats = status?.stats || {};
  const engine = stats.engine || {};
  const scenarioId = status?.config?.scenario || ids.scenario.value;
  const health = state.health || {};
  const auth = health.auth || {};
  const authState = auth.enabled ? 'Enabled' : 'Off';
  const storageScope = auth.uses_default_storage === false ? 'Tenant' : 'Local';
  const cards = [
    ['Loop status', status?.running ? 'Running' : 'Stopped'],
    ['Scenario', scenarioLabel(scenarioId)],
    ['Operation size', engine.scale ?? 'midsize'],
    ['Facilities', engine.locations ?? '—'],
    ['Total records', stats.total_records ?? 0],
    ['Unique lots', stats.unique_lots ?? 0],
    ['Auth', authState],
    ['Storage', storageScope],
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
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </article>
      `,
    )
    .join('');
}

export function renderDeliverySummary(status) {
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
  const recoveryHint = deliveryRecoveryHint(state.events);
  ids.deliverySummary.innerHTML = `
    <div class="delivery-cards">
      ${cards
        .map(
          ([label, value, tone]) => `
            <article class="delivery-card" data-tone="${escapeHtml(tone)}">
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
      ${
        recoveryHint
          ? `
            <div>
              <dt>How to recover</dt>
              <dd>${escapeHtml(recoveryHint)}</dd>
            </div>
          `
          : ''
      }
    </dl>
  `;
}

export function deliveryRecoveryHint(events = []) {
  const failedRecord = (events || []).find((record) => record.delivery_status === 'failed');
  if (!failedRecord) {
    return '';
  }
  const statusCode = failedRecord.delivery_metadata?.status_code;
  if (statusCode && DELIVERY_RECOVERY_HINTS[statusCode]) {
    return DELIVERY_RECOVERY_HINTS[statusCode];
  }
  if ((failedRecord.error || '').includes('Missing required KDEs')) {
    return 'RegEngine rejected the record for missing KDEs. Fix the source data (the errors name the exact fields), then retry or re-import.';
  }
  if ((failedRecord.error || '').includes('Duplicate event')) {
    return 'RegEngine saw a duplicate of this event in the same batch — it was already recorded.';
  }
  return 'Check the error detail, fix the cause, then use Retry failed. Retries reuse the original idempotency key so nothing double-ingests.';
}

export function pickSpotlightRecord(events) {
  if (!events.length) {
    return null;
  }
  return (
    events.find((record) => record.event.cte_type === 'transformation') ||
    events.find((record) => record.event.cte_type === 'shipping') ||
    events[0]
  );
}

export function spotlightFields(event) {
  const preferredKeys = [
    'reference_document',
    'reference_document_number',
    'vessel_identifier',
    'vessel_name',
    'landing_date',
    'field_gps_coordinates',
    'plu_code',
    'flow_type',
    'silo_identifier',
    'vat_identifier',
    'packaging_hierarchy',
    'packaging_conversion',
    'input_traceability_lot_codes',
    'output_traceability_lot_codes',
    'rework_traceability_lot_codes',
    'yield_ratio',
    'sscc',
  ];
  const entries = [];
  for (const key of preferredKeys) {
    if (Object.prototype.hasOwnProperty.call(event.kdes || {}, key)) {
      entries.push([key, event.kdes[key]]);
    }
  }
  if (!entries.length) {
    return Object.entries(event.kdes || {}).slice(0, 8);
  }
  return entries.slice(0, 8);
}

export function renderRecordSpotlight(events) {
  const record = pickSpotlightRecord(events || []);
  if (!record) {
    ids.recordSpotlight.innerHTML = '<p class="note">Run the pipeline or load a fixture to inspect an audit-style record spotlight.</p>';
    return;
  }
  const event = record.event;
  const keyFacts = [
    ['CTE', cteLabel(event.cte_type)],
    ['Lot', event.traceability_lot_code],
    ['Quantity', `${event.quantity} ${event.unit_of_measure}`],
    ['Location', event.location_name],
    ['Posted status', record.delivery_status],
  ];
  const fields = spotlightFields(event);
  ids.recordSpotlight.innerHTML = `
    <div class="spotlight-hero">
      <div>
        <span class="pill">${escapeHtml(cteLabel(event.cte_type))}</span>
        <h3>${escapeHtml(event.product_description)}</h3>
        <p class="note">Sequence ${escapeHtml(record.sequence_no)} at ${formatDateTime(event.timestamp)}</p>
      </div>
      <button class="button secondary" type="button" data-spotlight-lot="${escapeHtml(event.traceability_lot_code)}">Trace this lot</button>
    </div>
    <div class="spotlight-facts">
      ${keyFacts
        .map(
          ([label, value]) => `
            <article class="spotlight-fact">
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </article>
          `,
        )
        .join('')}
    </div>
    <div class="spotlight-kdes">
      ${fields
        .map(
          ([key, value]) => `
            <article class="spotlight-kde">
              <span>${escapeHtml(key)}</span>
              <strong>${escapeHtml(formatKdeValue(value))}</strong>
            </article>
          `,
        )
        .join('')}
    </div>
  `;
  ids.recordSpotlight.querySelector('[data-spotlight-lot]')?.addEventListener('click', async (eventNode) => {
    ids.lotLookup.value = eventNode.currentTarget.dataset.spotlightLot;
    await lookupLineage();
  });
}

export function renderImportResult(result) {
  const tone = result.status === 'accepted' ? 'success' : result.status === 'delivery_failed' ? 'error' : 'neutral';
  const errors = (result.errors || []).slice(0, 8);
  const warnings = (result.warnings || []).slice(0, 8);
  const errorList = errors
    .map((error) => {
      const field = error.field ? ` ${escapeHtml(error.field)}:` : '';
      return `<li>Row ${escapeHtml(error.row)}${field} ${escapeHtml(error.message)}</li>`;
    })
    .join('');
  const warningList = warnings
    .map((warning) => {
      const field = warning.field ? ` ${escapeHtml(warning.field)}:` : '';
      return `<li>Row ${escapeHtml(warning.row)}${field} ${escapeHtml(warning.message)}</li>`;
    })
    .join('');
  ids.importResults.innerHTML = `
    <div class="import-summary" data-tone="${escapeHtml(tone)}">
      Accepted ${escapeHtml(result.accepted)} of ${escapeHtml(result.total)} row(s).
      Stored ${escapeHtml(result.stored)}; posted ${escapeHtml(result.posted)}; rejected ${escapeHtml(result.rejected)}.
      ${result.error ? `<span>${escapeHtml(result.error)}</span>` : ''}
    </div>
    ${errorList ? `<ul>${errorList}</ul>` : ''}
    ${warningList ? `<ul>${warningList}</ul>` : ''}
  `;
}

export function renderConnectionStatus(integration) {
  if (!ids.connectionChips || !integration) {
    return;
  }
  state.integration = integration;
  adoptIntegrationDefaults(integration);
  const chips = [
    ['Endpoint', integration.endpoint_host || 'default'],
    ['API key', integration.api_key_configured ? 'Configured' : 'Not set'],
    ['Tenant', integration.tenant_configured ? 'Configured' : 'Not set'],
    ['HMAC signing', integration.hmac_configured ? 'On' : 'Off'],
    ['Contract', integration.contract_version ? `v${integration.contract_version}` : 'n/a'],
  ];
  ids.connectionChips.innerHTML = chips
    .map(
      ([label, value]) => `
        <span class="connection-chip">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </span>
      `,
    )
    .join('');
  if (Array.isArray(integration.mock_friction)) {
    if (ids.frictionInvalidKey) ids.frictionInvalidKey.checked = integration.mock_friction.includes('invalid_key');
    if (ids.frictionSubscription) ids.frictionSubscription.checked = integration.mock_friction.includes('subscription_inactive');
    if (ids.frictionRateLimit) ids.frictionRateLimit.checked = integration.mock_friction.includes('rate_limit');
  }
}

export async function loadIntegrationStatus() {
  const integration = await api('/api/integration/status');
  renderConnectionStatus(integration);
}

export async function hydrateIntegrationStatus() {
  try {
    const integration = await api('/api/integration/status');
    state.integration = integration;
    renderConnectionStatus(integration);
  } catch (error) {
    // Chip freshness is best-effort; the snapshot itself already rendered.
  }
}

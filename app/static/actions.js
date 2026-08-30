// The handlers behind the console's buttons.
//
// Every one is wrapped in `action()`, which is the single place a failed action
// is reported. Before this, that four-line catch block was written out twenty-one
// times.

import { CONNECTION_VERDICT_TONES, journey, state } from './state.js';
import { ids, setStatus } from './dom.js';
import { escapeHtml } from './format.js';
import { api, buildConfig, frictionSelections } from './api.js';
import { applyConfigToForm, loadScenarioSaves, preferredTraceLot, renderConnectionStatus, renderGuide, renderImportResult, renderLineage, renderScenarioOptions, updateExportLink, updateScenarioSaveDescription } from './render.js';
import { refresh } from './stream.js';

// Every action handler ended in the same four lines: catch the error, put its
// message in the status line, hold it for five seconds. That block appeared
// twenty-one times, so any change to how failures are reported meant editing
// sixteen call sites. `action()` is now the only place it lives. Two handlers
// still catch, because they paint the failure into their own panel first --
// they rethrow, so the reporting stays here.
export function action(handler) {
  return async (...args) => {
    try {
      return await handler(...args);
    } catch (error) {
      setStatus(error.message, 'error', 5000);
      return undefined;
    }
  };
}

export const saveIntegrationSettings = action(async () => {
  const request = {
    mode: ids.deliveryMode.value,
    mock_friction: frictionSelections(),
  };
  const endpoint = ids.endpoint.value.trim();
  if (endpoint) {
    request.endpoint = endpoint;
  }
  const apiKey = ids.apiKey.value.trim();
  if (apiKey) {
    request.api_key = apiKey;
  }
  const tenantId = ids.tenantId.value.trim();
  if (tenantId) {
    request.tenant_id = tenantId;
  }
  const integration = await api('/api/integration/configure', {
    method: 'POST',
    body: JSON.stringify(request),
  });
  renderConnectionStatus(integration);
  setStatus('Saved RegEngine connection settings.', 'success', 2500);
  await refresh();
});

export const testConnection = action(async () => {
  if (ids.connectionResult) {
    ids.connectionResult.innerHTML = '<p class="note">Testing connection…</p>';
  }
  try {
    const request = {};
    const endpoint = ids.endpoint.value.trim();
    if (endpoint) {
      request.endpoint = endpoint;
    }
    const apiKey = ids.apiKey.value.trim();
    if (apiKey) {
      request.api_key = apiKey;
    }
    const tenantId = ids.tenantId.value.trim();
    if (tenantId) {
      request.tenant_id = tenantId;
    }
    const result = await api('/api/integration/test', {
      method: 'POST',
      body: JSON.stringify(request),
    });
    const tone = CONNECTION_VERDICT_TONES[result.verdict] || 'neutral';
    const statusLine = result.status_code ? ` (HTTP ${result.status_code})` : '';
    if (ids.connectionResult) {
      ids.connectionResult.innerHTML = `
        <div class="connection-verdict" data-tone="${escapeHtml(tone)}">
          <strong>${escapeHtml(result.verdict.replaceAll('_', ' '))}${escapeHtml(statusLine)}</strong>
          <p>${escapeHtml(result.detail)}</p>
        </div>
      `;
    }
    setStatus(`Connection test: ${result.verdict.replaceAll('_', ' ')}.`, tone === 'error' ? 'error' : 'success', 4000);
  } catch (error) {
    if (ids.connectionResult) {
      ids.connectionResult.innerHTML = `<div class="connection-verdict" data-tone="error"><p>${escapeHtml(error.message)}</p></div>`;
    }
    throw error;
  }
});

export const startLoop = action(async () => {
  await api('/api/simulate/start', {
    method: 'POST',
    body: JSON.stringify({ config: buildConfig() }),
  });
  setStatus('Started production line.', 'success', 2500);
  await refresh();
});

export const stopLoop = action(async () => {
  await api('/api/simulate/stop', { method: 'POST' });
  setStatus('Paused production line.', 'success', 2500);
  await refresh();
});

export const stepOnce = action(async () => {
  const result = await api('/api/simulate/step', { method: 'POST' });
  const traceLot = preferredTraceLot(result.lot_codes || []);
  if (traceLot) {
    ids.lotLookup.value = traceLot;
    ids.exportLot.value = traceLot;
    updateExportLink();
  }
  if (result.delivery_status === 'failed') {
    setStatus(`Recorded ${result.generated} event(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
  } else if (result.delivery_status === 'generated') {
    setStatus(`Recorded ${result.generated} event(s) without delivery.`, 'success', 2500);
  } else if (result.rejected > 0) {
    setStatus(`Recorded ${result.generated} event(s); RegEngine accepted ${result.accepted} and rejected ${result.rejected}.`, 'error', 7000);
  } else {
    setStatus(`Recorded and posted ${result.posted} event(s).`, 'success', 2500);
  }
  await refresh();
});

export const retryFailedDeliveries = action(async () => {
  const config = buildConfig();
  const result = await api('/api/delivery/retry', {
    method: 'POST',
    body: JSON.stringify({ delivery: config.delivery, source: config.source }),
  });
  if (result.status === 'empty') {
    setStatus('No failed deliveries are waiting to retry.', 'success', 2500);
  } else if (result.status === 'skipped') {
    setStatus(result.error || 'Retry skipped.', 'error', 5000);
  } else if (result.failed > 0) {
    setStatus(`Retried ${result.attempted} record(s): ${result.posted} posted, ${result.failed} failed.`, 'error', 7000);
  } else {
    setStatus(`Retried and posted ${result.posted} failed delivery record(s).`, 'success', 3500);
  }
  await refresh();
});

export const saveCurrentScenario = action(async () => {
  const config = buildConfig();
  const result = await api(`/api/scenario-saves/${encodeURIComponent(config.scenario)}`, {
    method: 'POST',
    body: JSON.stringify({ config }),
  });
  await loadScenarioSaves();
  ids.scenarioSave.value = result.save.scenario;
  updateScenarioSaveDescription();
  setStatus(`Saved ${result.save.label} with ${result.save.record_count} event(s).`, 'success', 3500);
});

export const loadSavedScenario = action(async () => {
  const scenarioId = ids.scenarioSave.value;
  if (!scenarioId) {
  setStatus('Save a scenario first.', 'error', 5000);
  return;
  }
  const result = await api(`/api/scenario-saves/${encodeURIComponent(scenarioId)}/load`, {
    method: 'POST',
  });
  applyConfigToForm(result.config);
  ids.lineageResults.innerHTML = '';
  ids.importResults.innerHTML = '';
  await refresh();
  await loadScenarioSaves();
  ids.scenarioSave.value = result.save.scenario;
  updateScenarioSaveDescription();
  setStatus(`Loaded ${result.save.label} with ${result.loaded_records} saved event(s).`, 'success', 3500);
});

export const loadSelectedDemoFixture = action(async () => {
  const config = buildConfig();
  const fixtureId = ids.demoFixture.value || 'leafy_greens_trace';
  const result = await api(`/api/demo-fixtures/${encodeURIComponent(fixtureId)}/load`, {
    method: 'POST',
    body: JSON.stringify({
      reset: true,
      source: config.source,
      delivery: config.delivery,
    }),
  });
  const fixtureScenario = state.scenarioCatalog[result.scenario];
  renderScenarioOptions(
    state.allScenarios.length ? state.allScenarios : Object.values(state.scenarioCatalog),
    result.scenario,
    fixtureScenario?.operation_type || null,
  );
  ids.lineageResults.innerHTML = '';
  const traceLot = preferredTraceLot(result.lot_codes || []);
  if (traceLot) {
    ids.lotLookup.value = traceLot;
    ids.exportLot.value = traceLot;
    updateExportLink();
  }
  if (result.status === 'delivery_failed') {
    setStatus(`Loaded ${result.stored} line event(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
  } else if (result.delivery_mode === 'none') {
    setStatus(`Loaded ${result.stored} line event(s) without delivery.`, 'success', 3500);
  } else {
    setStatus(`Loaded line data and posted ${result.posted} event(s).`, 'success', 3500);
  }
  await refresh();
});

export const replayCurrentLog = action(async () => {
  const result = await api('/api/simulate/replay', { method: 'POST' });
  const path = result.persist_path || 'current log';
  if (result.status === 'empty') {
    setStatus(`No persisted events found at ${path}.`, 'error', 5000);
  } else if (result.status === 'failed') {
    setStatus(`Replay failed for ${result.failed} event(s): ${result.error || 'delivery error'}`, 'error', 5000);
  } else if (result.status === 'rebuilt') {
    setStatus(`Rebuilt ${result.replayed} event(s) from ${path}; delivery mode is none.`, 'success', 3500);
  } else {
    setStatus(`Replayed ${result.posted} event(s) from ${path}.`, 'success', 3500);
  }
  await refresh();
});

export const importCsv = action(async () => {
  const file = ids.csvFile.files[0];
  if (!file) {
  setStatus('Choose a CSV file first.', 'error', 5000);
  return;
  }
  const config = buildConfig();
  const result = await api('/api/import/csv', {
    method: 'POST',
    body: JSON.stringify({
      import_type: ids.csvImportType.value,
      csv_text: await file.text(),
      source: config.source,
      delivery: config.delivery,
    }),
  });
  renderImportResult(result);
  if (result.status === 'delivery_failed') {
    setStatus(`Imported ${result.accepted} row(s), but delivery failed: ${result.error || 'delivery error'}`, 'error', 7000);
  } else if (result.rejected > 0) {
    setStatus(`Imported ${result.accepted} row(s); rejected ${result.rejected}.`, 'error', 7000);
  } else if ((result.warnings || []).length > 0) {
    setStatus(`Imported ${result.accepted} CSV row(s) with ${result.warnings.length} warning(s).`, 'success', 4500);
  } else {
    setStatus(`Imported ${result.accepted} CSV row(s).`, 'success', 3500);
  }
  await refresh();
});

export const resetState = action(async () => {
  await api('/api/simulate/reset', {
    method: 'POST',
    body: JSON.stringify(buildConfig()),
  });
  ids.lineageResults.innerHTML = '';
  setStatus('Cleared line state and shift log.', 'success', 2500);
  await refresh();
});

export const lookupLineage = action(async () => {
  const lotCode = ids.lotLookup.value.trim();
  if (!lotCode) {
    setStatus('Enter a lot code first.', 'error', 5000);
    return;
  }
  try {
    const payload = await api(`/api/lineage/${encodeURIComponent(lotCode)}`);
    renderLineage(payload, lotCode);
    journey.traced = true;
    renderGuide();
    setStatus(`Loaded lineage for ${lotCode}.`, 'success', 2500);
  } catch (error) {
    ids.lineageResults.innerHTML = `<p class="note">${escapeHtml(error.message)}</p>`;
    throw error;
  }
});

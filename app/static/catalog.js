// Everything the setup panel picks from: scenario presets filtered by
// operation type, saved scenarios, and demo fixtures.

import { ids, state } from './dom.js';
import { escapeHtml } from './format.js';
import { operationTypeLabel } from './labels.js';
import { api } from './api.js';
import { activeScenarioSummary, renderReadinessBanner, renderScenarioWorkbench } from './audit.js';
import { renderRecordSpotlight } from './panels.js';

export function scenariosForOperation(operationType = ids.operationType.value || 'all') {
  if (operationType === 'all') {
    return state.allScenarios;
  }
  return state.allScenarios.filter((scenario) => scenario.operation_type === operationType);
}

export function renderOperationTypeOptions(scenarios, preferredOperationType = ids.operationType.value || 'all') {
  const operationTypes = ['all', ...new Set(scenarios.map((scenario) => scenario.operation_type))];
  ids.operationType.innerHTML = operationTypes
    .map(
      (operationType) => `
        <option value="${escapeHtml(operationType)}">${escapeHtml(operationTypeLabel(operationType))}</option>
      `,
    )
    .join('');
  ids.operationType.value = operationTypes.includes(preferredOperationType) ? preferredOperationType : 'all';
  updateOperationTypeDescription();
}

export function updateOperationTypeDescription() {
  const operationType = ids.operationType.value || 'all';
  if (operationType === 'all') {
    ids.operationTypeDescription.textContent =
      'Choose the type of operation you run to narrow the scenario presets to matching flows.';
    return;
  }
  const visibleCount = scenariosForOperation(operationType).length;
  ids.operationTypeDescription.textContent =
    `${operationTypeLabel(operationType)} view: showing ${visibleCount} matching scenario${visibleCount === 1 ? '' : 's'} you can start from.`;
}

export function renderScenarioOptions(
  scenarios,
  preferredScenarioId = ids.scenario.value || 'leafy_greens_supplier',
  preferredOperationType = null,
) {
  state.allScenarios = scenarios;
  state.scenarioCatalog = Object.fromEntries(scenarios.map((scenario) => [scenario.id, scenario]));
  state.scenarioLabels = Object.fromEntries(scenarios.map((scenario) => [scenario.id, scenario.label]));
  const selectedScenario = state.scenarioCatalog[preferredScenarioId] || null;
  renderOperationTypeOptions(
    scenarios,
    preferredOperationType || selectedScenario?.operation_type || ids.operationType.value || 'all',
  );
  const filteredScenarios = scenariosForOperation();
  ids.scenario.innerHTML = filteredScenarios
    .map(
      (scenario) => `
        <option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.label)}</option>
      `,
    )
    .join('');
  ids.scenario.value = filteredScenarios.some((scenario) => scenario.id === preferredScenarioId)
    ? preferredScenarioId
    : filteredScenarios[0]?.id || scenarios[0]?.id || 'leafy_greens_supplier';
  renderReadinessBanner(activeScenarioSummary(), state.events, state.status);
  renderScenarioWorkbench(state.status, state.events);
  renderRecordSpotlight(state.events);
}

export async function loadScenarios() {
  const payload = await api('/api/scenarios');
  renderScenarioOptions(payload.scenarios || []);
}

export function applyConfigToForm(config) {
  if (!config) {
    return;
  }
  ids.source.value = config.source || 'codex-simulator';
  if (ids.operationScale) {
    ids.operationScale.value = config.scale || 'midsize';
  }
  const scenarioId = config.scenario || 'leafy_greens_supplier';
  const scenario = state.scenarioCatalog[scenarioId];
  renderScenarioOptions(
    state.allScenarios.length ? state.allScenarios : Object.values(state.scenarioCatalog),
    scenarioId,
    scenario?.operation_type || null,
  );
  ids.interval.value = config.interval_seconds ?? 1.5;
  ids.batchSize.value = config.batch_size ?? 3;
  ids.seed.value = config.seed ?? '';
  ids.deliveryMode.value = config.delivery?.mode || 'mock';
  ids.endpoint.value = config.delivery?.endpoint || '';
  ids.apiKey.value = '';
  ids.tenantId.value = config.delivery?.tenant_id || '';
}

export function renderScenarioSaveOptions(saves) {
  const selected = ids.scenarioSave.value;
  state.scenarioSaves = saves || [];
  if (!state.scenarioSaves.length) {
    ids.scenarioSave.innerHTML = '<option value="">No saved scenarios</option>';
    ids.scenarioSave.value = '';
    ids.loadScenarioBtn.disabled = true;
    updateScenarioSaveDescription();
    return;
  }
  ids.scenarioSave.innerHTML = state.scenarioSaves
    .map(
      (save) => `
        <option value="${escapeHtml(save.scenario)}">${escapeHtml(save.label)}</option>
      `,
    )
    .join('');
  ids.scenarioSave.value = state.scenarioSaves.some((save) => save.scenario === selected)
    ? selected
    : state.scenarioSaves[0].scenario;
  ids.loadScenarioBtn.disabled = false;
  updateScenarioSaveDescription();
}

export async function loadScenarioSaves() {
  const payload = await api('/api/scenario-saves');
  renderScenarioSaveOptions(payload.saves || []);
}

export function updateScenarioSaveDescription() {
  const selected = ids.scenarioSave.value;
  const save = state.scenarioSaves.find((item) => item.scenario === selected);
  if (!save) {
    ids.scenarioSaveDescription.textContent = 'Save the current scenario controls and event log for later demos.';
    return;
  }
  const savedAt = new Date(save.saved_at).toLocaleString();
  ids.scenarioSaveDescription.textContent = `${save.label}: ${save.record_count} event(s), ${save.lot_codes.length} lot(s), saved ${savedAt}.`;
}

export function renderDemoFixtureOptions(fixtures) {
  const selected = ids.demoFixture.value || 'leafy_greens_trace';
  state.demoFixtureDescriptions = Object.fromEntries(
    fixtures.map((fixture) => [fixture.id, fixture.description]),
  );
  ids.demoFixture.innerHTML = fixtures
    .map(
      (fixture) => `
        <option value="${escapeHtml(fixture.id)}">${escapeHtml(fixture.label)}</option>
      `,
    )
    .join('');
  ids.demoFixture.value = state.demoFixtureDescriptions[selected] ? selected : fixtures[0]?.id || 'leafy_greens_trace';
  updateDemoFixtureDescription();
}

export async function loadDemoFixtures() {
  const payload = await api('/api/demo-fixtures');
  renderDemoFixtureOptions(payload.fixtures || []);
}

export function updateDemoFixtureDescription() {
  const fixtureId = ids.demoFixture.value || 'leafy_greens_trace';
  ids.demoFixtureDescription.textContent = state.demoFixtureDescriptions[fixtureId] || 'Deterministic demo fixture.';
}

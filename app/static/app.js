// Operator console entry point.
//
// This file used to be the whole console: state, the DOM lookup table, the
// onboarding tour, a fetch wrapper, ~15 render functions, SSE streaming and
// every action handler in one 1,830-line script (issue #154). It is now the
// wiring layer only — the modules below own the behavior, and this file binds
// them to the DOM and boots the first snapshot.
//
// The import graph is acyclic and dependency-free: dom/format/api/ui/config at
// the bottom, panels and actions at the top, so any module can be read on its
// own. Modules are loaded natively (<script type="module">), no build step.
import { ids, state } from './dom.js';
import { bindAsyncClick, flashPanel, reportActionError, runWithBusy, setStatus } from './ui.js';
import { goToNextAction } from './guide.js';
import {
  advanceTour,
  endTour,
  hideWelcome,
  maybeShowWelcome,
  startTour,
  tour,
  tourEls,
  welcomeEls,
} from './onboarding.js';
import { activeScenarioSummary, renderReadinessBanner, renderScenarioWorkbench } from './audit.js';
import { loadIntegrationStatus, renderRecordSpotlight, updateShellStatus } from './panels.js';
import { renderEvents } from './shift-log.js';
import { lookupLineage } from './lineage.js';
import { downloadExport, loadExportPresets, updateExportLink } from './exports.js';
import {
  loadDemoFixtures,
  loadScenarioSaves,
  loadScenarios,
  renderScenarioOptions,
  updateDemoFixtureDescription,
  updateScenarioSaveDescription,
} from './catalog.js';
import { connectLiveUpdates, refresh, startFallbackPolling } from './snapshot.js';
import {
  importCsv,
  loadSavedScenario,
  loadSelectedDemoFixture,
  replayCurrentLog,
  resetState,
  retryFailedDeliveries,
  saveCurrentScenario,
  saveIntegrationSettings,
  startLoop,
  stepOnce,
  stopLoop,
  testConnection,
} from './actions.js';

// One delegated listener for the whole table instead of one per row rebound
// on every snapshot.
ids.eventsBody.addEventListener('click', (event) => {
  const button = event.target.closest('[data-lot]');
  if (!button || !ids.eventsBody.contains(button)) {
    return;
  }
  ids.lotLookup.value = button.dataset.lot;
  runWithBusy(ids.lineageBtn, lookupLineage).catch(reportActionError);
});

bindAsyncClick(ids.startBtn, startLoop);
bindAsyncClick(ids.stopBtn, stopLoop);
bindAsyncClick(ids.stepBtn, stepOnce);
bindAsyncClick(ids.replayBtn, replayCurrentLog);
bindAsyncClick(ids.importCsvBtn, importCsv);
bindAsyncClick(ids.retryFailedBtn, retryFailedDeliveries);
bindAsyncClick(ids.saveScenarioBtn, saveCurrentScenario);
bindAsyncClick(ids.loadScenarioBtn, loadSavedScenario);
bindAsyncClick(ids.loadFixtureBtn, loadSelectedDemoFixture);
bindAsyncClick(ids.refreshBtn, refresh);
bindAsyncClick(ids.lineageBtn, lookupLineage);
bindAsyncClick(ids.testConnectionBtn, testConnection);
bindAsyncClick(ids.saveIntegrationBtn, saveIntegrationSettings);

// Clear shift is destructive, so the first click arms the button and only a
// second click within the arm window actually wipes the workspace.
let resetArmTimer = null;
function disarmReset() {
  if (resetArmTimer) {
    clearTimeout(resetArmTimer);
    resetArmTimer = null;
  }
  ids.resetBtn.classList.remove('is-armed');
  ids.resetBtn.textContent = 'Clear shift';
}
ids.resetBtn.addEventListener('click', () => {
  if (ids.resetBtn.dataset.busy === '1') {
    return;
  }
  if (!ids.resetBtn.classList.contains('is-armed')) {
    ids.resetBtn.classList.add('is-armed');
    ids.resetBtn.textContent = 'Confirm clear shift';
    setStatus('Clearing wipes every record in this workspace. Click Confirm clear shift to proceed.', 'neutral', 6000);
    resetArmTimer = setTimeout(disarmReset, 6000);
    return;
  }
  disarmReset();
  // Not bound through bindAsyncClick, so it needs the shared reporter here.
  runWithBusy(ids.resetBtn, resetState).catch(reportActionError);
});

// The setup grid is a <form>; without this, pressing Enter in a text field
// triggers the implicit submission and reloads the whole page.
document.getElementById('config-form').addEventListener('submit', (event) => event.preventDefault());

ids.lotLookup.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    runWithBusy(ids.lineageBtn, lookupLineage).catch(reportActionError);
  }
});

document.getElementById('nextActionGo')?.addEventListener('click', goToNextAction);
for (const [link, label] of [
  [ids.exportDownloadLink, 'Compliance export'],
  [ids.epcisDownloadLink, 'EPCIS JSON'],
  [ids.navExportLink, 'Compliance export'],
]) {
  link?.addEventListener('click', (event) => {
    event.preventDefault();
    downloadExport(link, label);
  });
}
document.querySelectorAll('#guideRail [data-guide-target]').forEach((step) => {
  step.addEventListener('click', () => flashPanel(step.dataset.guideTarget));
  step.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      flashPanel(step.dataset.guideTarget);
    }
  });
});

document.getElementById('tourBtn')?.addEventListener('click', startTour);
tourEls.next.addEventListener('click', () => advanceTour(1));
tourEls.back.addEventListener('click', () => advanceTour(-1));
tourEls.skip.addEventListener('click', endTour);
welcomeEls.tour.addEventListener('click', () => {
  hideWelcome();
  startTour();
});
welcomeEls.sample.addEventListener('click', () => {
  hideWelcome();
  runWithBusy(ids.loadFixtureBtn, loadSelectedDemoFixture).catch(reportActionError);
  flashPanel('.run-panel');
});
welcomeEls.skip.addEventListener('click', hideWelcome);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') {
    return;
  }
  if (!welcomeEls.overlay.hidden) {
    hideWelcome();
  } else if (tour.active) {
    endTour();
  }
});
ids.scenarioSave.addEventListener('change', updateScenarioSaveDescription);
ids.demoFixture.addEventListener('change', updateDemoFixtureDescription);
ids.exportPreset.addEventListener('change', updateExportLink);
ids.exportLot.addEventListener('input', updateExportLink);
ids.exportStartDate.addEventListener('change', updateExportLink);
ids.exportEndDate.addEventListener('change', updateExportLink);
ids.deliveryMode.addEventListener('change', () => updateShellStatus());
ids.operationType.addEventListener('change', () => {
  renderScenarioOptions(state.allScenarios, ids.scenario.value, ids.operationType.value);
});
ids.scenario.addEventListener('change', () => {
  renderReadinessBanner(activeScenarioSummary(), state.events, state.status);
  renderScenarioWorkbench(state.status, state.events);
  renderRecordSpotlight(state.events);
  renderEvents(state.events);
});

loadScenarios().catch(reportActionError);
loadScenarioSaves().catch(reportActionError);
loadDemoFixtures().catch(reportActionError);
loadExportPresets().catch(reportActionError);
loadIntegrationStatus().catch(reportActionError);
connectLiveUpdates();
refresh().then((ok) => {
  if (!ok) {
    startFallbackPolling();
  }
});
maybeShowWelcome();

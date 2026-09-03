import { ids, state } from './dom.js';
import { setStatus, flashPanel, runWithBusy, bindAsyncClick, reportActionError } from './ui.js';
import {
  saveIntegrationSettings,
  testConnection,
  startLoop,
  stopLoop,
  stepOnce,
  retryFailedDeliveries,
  saveCurrentScenario,
  loadSavedScenario,
  loadSelectedDemoFixture,
  replayCurrentLog,
  importCsv,
  resetState,
} from './actions.js';
import { lookupLineage } from './lineage.js';
import { refresh, startFallbackPolling, connectLiveUpdates } from './snapshot.js';
import {
  loadScenarios,
  loadScenarioSaves,
  loadDemoFixtures,
  updateScenarioSaveDescription,
  updateDemoFixtureDescription,
  renderScenarioOptions,
} from './catalog.js';
import { loadExportPresets, updateExportLink, downloadExport } from './exports.js';
import { updateShellStatus, loadIntegrationStatus, renderRecordSpotlight } from './panels.js';
import { activeScenarioSummary, renderReadinessBanner, renderScenarioWorkbench } from './audit.js';
import { renderEvents } from './shift-log.js';
import { goToNextAction } from './guide.js';
import {
  startTour,
  advanceTour,
  endTour,
  hideWelcome,
  maybeShowWelcome,
  tour,
  tourEls,
  welcomeEls,
} from './onboarding.js';

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
    setStatus(
      'Clearing wipes every record in this workspace. Click Confirm clear shift to proceed.',
      'neutral',
      6000,
    );
    resetArmTimer = setTimeout(disarmReset, 6000);
    return;
  }
  disarmReset();
  runWithBusy(ids.resetBtn, resetState).catch(reportActionError);
});

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

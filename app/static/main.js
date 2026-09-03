// Wiring. Every listener the console installs, and the bootstrap sequence
// that runs once the page loads. Nothing else in the console adds a
// listener, so this file is the whole answer to "what happens when I click
// that?".

import { journey, state } from './state.js';
import { bindAsyncClick, flashPanel, ids, onTraceLot, runWithBusy, setStatus, tourEls, welcomeEls } from './dom.js';
import { advanceTour, endTour, hideWelcome, maybeShowWelcome, startTour, tour } from './onboarding.js';
import { activeScenarioSummary, goToNextAction, loadDemoFixtures, loadExportPresets, loadIntegrationStatus, loadScenarioSaves, loadScenarios, renderEvents, renderGuide, renderReadinessBanner, renderRecordSpotlight, renderScenarioOptions, renderScenarioWorkbench, updateDemoFixtureDescription, updateExportLink, updateScenarioSaveDescription, updateShellStatus } from './render.js';
import { connectLiveUpdates, refresh, startFallbackPolling } from './stream.js';
import { action, importCsv, loadSavedScenario, loadSelectedDemoFixture, lookupLineage, replayCurrentLog, resetState, retryFailedDeliveries, saveCurrentScenario, saveIntegrationSettings, startLoop, stepOnce, stopLoop, testConnection } from './actions.js';

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
onTraceLot(lookupLineage);
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
    setStatus('Clearing wipes every record in this workspace. Click Confirm clear shift to proceed.', 'neutral', 6000);
    resetArmTimer = setTimeout(disarmReset, 6000);
    return;
  }
  disarmReset();
  runWithBusy(ids.resetBtn, resetState);
});

// The setup grid is a <form>; without this, pressing Enter in a text field
// triggers the implicit submission and reloads the whole page.
document.getElementById('config-form').addEventListener('submit', (event) => event.preventDefault());

ids.lotLookup.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    runWithBusy(ids.lineageBtn, lookupLineage);
  }
});

document.getElementById('nextActionGo')?.addEventListener('click', goToNextAction);
for (const link of [ids.exportDownloadLink, ids.epcisDownloadLink]) {
  link?.addEventListener('click', () => {
    if (!state.events.length) {
      return;
    }
    journey.exported = true;
    renderGuide();
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
  runWithBusy(ids.loadFixtureBtn, loadSelectedDemoFixture);
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

for (const load of [
  loadScenarios,
  loadScenarioSaves,
  loadDemoFixtures,
  loadExportPresets,
  loadIntegrationStatus,
]) {
  action(load)();
}
connectLiveUpdates();
action(async () => {
  try {
    await refresh();
  } catch (error) {
    // Losing the first refresh means the stream is probably unavailable too,
    // so fall back to polling before letting the wrapper report the failure.
    startFallbackPolling();
    throw error;
  }
})();
maybeShowWelcome();

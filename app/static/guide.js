// The "How to use this console" stepper and the next-action shortcut.

import { ids, state } from './dom.js';
import { flashPanel } from './ui.js';

// Progress through the guided flow, used to light up the "How to use this
// console" stepper. traced/exported are set by user actions; the rest are
// derived from live state on every snapshot.
export const journey = { traced: false, exported: false };

export function renderGuide(status = state.status, events = state.events) {
  const rail = document.getElementById('guideRail');
  if (!rail) {
    return;
  }
  const hasEvents = (events || []).length > 0;
  const delivery = status?.stats?.delivery || {};
  const delivered = Number(delivery.posted || 0) > 0 || Number(delivery.failed || 0) > 0;
  const done = {
    setup: hasEvents,
    data: hasEvents,
    delivery: delivered,
    trace: journey.traced,
    export: journey.exported,
  };
  let currentAssigned = false;
  rail.querySelectorAll('[data-guide-step]').forEach((step) => {
    const key = step.dataset.guideStep;
    if (done[key]) {
      step.dataset.state = 'done';
      step.removeAttribute('aria-current');
    } else if (!currentAssigned) {
      step.dataset.state = 'current';
      // Progress state was CSS-only; expose it so assistive tech hears
      // "you are here" the same way sighted users see it.
      step.setAttribute('aria-current', 'step');
      currentAssigned = true;
    } else {
      step.dataset.state = '';
      step.removeAttribute('aria-current');
    }
  });
}

// Where "Take me there" should send the user for each next-action label.
export const NEXT_ACTION_TARGETS = {
  'Verify RegEngine connection': '.integration-panel',
  'Retry failed deliveries': '.delivery-monitor',
  'Load line data': '.run-panel',
  'Trace a lot': '.lineage-panel',
  'Export evidence': '.evidence-panel',
};

export function goToNextAction() {
  const label = ids.nextActionText.textContent.trim();
  flashPanel(NEXT_ACTION_TARGETS[label] || '.run-panel');
  if (label === 'Trace a lot') {
    ids.lotLookup.focus();
  }
}

// First-run welcome dialog and the guided tour, including the modal focus
// trap the welcome overlay's role="dialog" aria-modal="true" promises.

import { setStatus } from './ui.js';

// --- Onboarding: first-run welcome + guided tour -------------------------
// Seen-state lives in localStorage so the welcome only interrupts once per
// browser; private-mode storage failures degrade to "never show".
export const ONBOARDING_KEY = 'inflowLab.onboarded.v1';

export function onboardingSeen() {
  try {
    return window.localStorage.getItem(ONBOARDING_KEY) === 'done';
  } catch (error) {
    return true;
  }
}

export function markOnboarded() {
  try {
    window.localStorage.setItem(ONBOARDING_KEY, 'done');
  } catch (error) {
    // Storage unavailable — the welcome simply shows again next visit.
  }
}

// Fuller copy than the guide rail's one-liners: each tour stop explains what
// the panel is for and what to actually click.
export const TOUR_STEPS = [
  {
    target: '.compact-panel',
    title: 'Set up the production line',
    body: 'This panel decides what the plant makes. The line profile picks products, locations, and event flow; the operation size scales lot counts and site counts. The defaults are fine for a first run — you can change them any time.',
  },
  {
    target: '.run-panel',
    title: 'Bring in traceability data',
    body: 'Start line records events continuously, Record next batch (in the header) does one batch at a time, and Load today’s line data fills the shift with a ready-made trace instantly. Clear shift wipes the workspace when you want a fresh start.',
  },
  {
    target: '.delivery-monitor',
    title: 'Watch delivery to RegEngine',
    body: 'Every recorded event posts to RegEngine — the built-in sandbox until you connect a real workspace in the Integrations panel. Failures land here with recovery guidance, and Retry failed is always safe: retries reuse the original idempotency key.',
  },
  {
    target: '.lineage-panel',
    title: 'Trace a lot through the plant',
    body: 'Click any lot code in the shift log — or type one into the trace box at the top — to follow it backward to its origin and forward to wherever it went, including through transformations.',
  },
  {
    target: '.evidence-panel',
    title: 'Export audit evidence',
    body: 'When the shift log has records, download the FDA-request CSV (header button) or the EPCIS 2.0 JSON package. The filters narrow the export by preset, lot, or date range. That’s the whole loop — you’re ready to run it yourself.',
  },
];

export const tour = { active: false, index: 0 };

export const tourEls = {
  popover: document.getElementById('tourPopover'),
  progress: document.getElementById('tourProgress'),
  title: document.getElementById('tourTitle'),
  body: document.getElementById('tourBody'),
  back: document.getElementById('tourBackBtn'),
  next: document.getElementById('tourNextBtn'),
  skip: document.getElementById('tourSkipBtn'),
};

export function clearTourHighlight() {
  document.querySelectorAll('.tour-highlight').forEach((node) => node.classList.remove('tour-highlight'));
}

export function showTourStep(index) {
  tour.index = Math.min(Math.max(index, 0), TOUR_STEPS.length - 1);
  const step = TOUR_STEPS[tour.index];
  tourEls.progress.textContent = `Step ${tour.index + 1} of ${TOUR_STEPS.length}`;
  tourEls.title.textContent = step.title;
  tourEls.body.textContent = step.body;
  tourEls.back.disabled = tour.index === 0;
  tourEls.next.textContent = tour.index === TOUR_STEPS.length - 1 ? 'Finish' : 'Next';
  clearTourHighlight();
  const target = document.querySelector(step.target);
  if (target) {
    target.classList.add('tour-highlight');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

export function startTour() {
  tour.active = true;
  tourEls.popover.hidden = false;
  showTourStep(0);
  tourEls.next.focus();
}

export function endTour() {
  tour.active = false;
  tourEls.popover.hidden = true;
  clearTourHighlight();
  markOnboarded();
  document.getElementById('tourBtn')?.focus();
}

export function advanceTour(delta) {
  const next = tour.index + delta;
  if (next >= TOUR_STEPS.length) {
    endTour();
    setStatus('Tour finished — load line data or record a batch to see it all in action.', 'success', 5000);
    return;
  }
  showTourStep(next);
}

export const welcomeEls = {
  overlay: document.getElementById('welcomeOverlay'),
  tour: document.getElementById('welcomeTourBtn'),
  sample: document.getElementById('welcomeSampleBtn'),
  skip: document.getElementById('welcomeSkipBtn'),
};

// The overlay declares role="dialog" aria-modal="true", so it has to behave
// like one: Tab and Shift+Tab cycle inside the card instead of wandering into
// the form hidden behind the backdrop. (The background is deliberately left
// out of `inert`/`aria-hidden` — aria-modal already conveys modality, and
// hiding the whole page from the accessibility tree would take the console's
// own landmarks with it.)
export function welcomeFocusables() {
  return [welcomeEls.tour, welcomeEls.sample, welcomeEls.skip].filter(
    (node) => node && !node.disabled && !node.hidden,
  );
}

export function trapWelcomeFocus(event) {
  if (event.key !== 'Tab' || welcomeEls.overlay.hidden) {
    return;
  }
  const focusables = welcomeFocusables();
  if (!focusables.length) {
    return;
  }
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (!welcomeEls.overlay.contains(active)) {
    event.preventDefault();
    first.focus();
    return;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

export function hideWelcome() {
  welcomeEls.overlay.hidden = true;
  document.removeEventListener('keydown', trapWelcomeFocus, true);
  markOnboarded();
}

export function maybeShowWelcome() {
  if (onboardingSeen()) {
    return;
  }
  welcomeEls.overlay.hidden = false;
  document.addEventListener('keydown', trapWelcomeFocus, true);
  welcomeEls.tour.focus();
}

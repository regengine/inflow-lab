// First-run welcome overlay and the guided tour.
//
// Self-contained: nothing else in the console reads its state, and it reads
// nothing but `ids` and the tour step table.

import { ONBOARDING_KEY, TOUR_STEPS } from './state.js';
import { setStatus, tourEls, welcomeEls } from './dom.js';

export const tour = { active: false, index: 0 };

export function onboardingSeen() {
  try {
    return window.localStorage.getItem(ONBOARDING_KEY) === 'done';
  } catch {
    return true;
  }
}

export function markOnboarded() {
  try {
    window.localStorage.setItem(ONBOARDING_KEY, 'done');
  } catch {
    // Storage unavailable — the welcome simply shows again next visit.
  }
}

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

export function hideWelcome() {
  welcomeEls.overlay.hidden = true;
  markOnboarded();
}

export function maybeShowWelcome() {
  if (onboardingSeen()) {
    return;
  }
  welcomeEls.overlay.hidden = false;
  welcomeEls.tour.focus();
}

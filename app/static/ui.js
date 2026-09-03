// The console's shared feedback surfaces: the status live region, panel
// flashing, and the button busy/bind plumbing that every action goes
// through — including the single place an unhandled action failure is
// reported.

import { ids, state } from './dom.js';

// #statusMessage is a live region (role="status" in index.html), so writing
// its text is enough to announce it. Errors interrupt; everything else waits
// for a pause in the screen reader's speech.
export function setStatus(message, tone = 'neutral', holdMs = 0) {
  ids.statusMessage.textContent = message;
  ids.statusMessage.dataset.tone = tone;
  ids.statusMessage.setAttribute('aria-live', tone === 'error' ? 'assertive' : 'polite');
  state.statusHoldUntil = holdMs > 0 ? Date.now() + holdMs : 0;
}

export function flashPanel(selector) {
  const panel = document.querySelector(selector);
  if (!panel) {
    return;
  }
  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
  panel.classList.remove('panel-flash');
  // Force a reflow so re-adding the class restarts the animation.
  void panel.offsetWidth;
  panel.classList.add('panel-flash');
}

// Keep Start/Pause honest about the loop state. Buttons mid-request keep
// their busy-disabled state until the request settles.
export function syncRunButtons(running = Boolean(state.status?.running)) {
  if (ids.startBtn.dataset.busy !== '1') {
    ids.startBtn.disabled = running;
  }
  if (ids.stopBtn.dataset.busy !== '1') {
    ids.stopBtn.disabled = !running;
  }
}

// Runs an async handler with the button disabled and spinning so double-clicks
// can't fire duplicate requests.
export async function runWithBusy(button, handler) {
  if (button.dataset.busy === '1') {
    return;
  }
  button.dataset.busy = '1';
  button.classList.add('is-busy');
  button.disabled = true;
  try {
    await handler();
  } finally {
    delete button.dataset.busy;
    button.classList.remove('is-busy');
    button.disabled = false;
    syncRunButtons();
  }
}

// The catch block that used to be copy-pasted at the end of every action
// handler. Handlers now let the error escape and this reports it once, so an
// error-UX change (telemetry, a different hold time) is a one-line edit.
export function reportActionError(error) {
  setStatus(error?.message || 'Action failed.', 'error', 5000);
}

// Every bound handler reports its own failures, but refresh() (and any
// future handler that forgets) must not leak an unhandled rejection: the
// operator would see a button spin and then silence.
export function bindAsyncClick(button, handler) {
  button?.addEventListener('click', () => {
    runWithBusy(button, handler).catch(reportActionError);
  });
}

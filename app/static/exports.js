// Evidence exports: preset options, the filtered download links, the guard
// that keeps a lot-requiring preset from opening a raw JSON 400, and the
// fetch-based download itself.

import { ids, state } from './dom.js';
import { escapeHtml } from './format.js';
import { api, errorMessageFor } from './api.js';
import { setStatus, flashPanel } from './ui.js';
import { journey, renderGuide } from './guide.js';

export function renderExportPresetOptions(presets) {
  const selected = ids.exportPreset.value || 'all_records';
  state.exportPresetDescriptions = Object.fromEntries(
    presets.map((preset) => [preset.id, preset.description]),
  );
  // The backend already advertises which presets need a Traceability Lot
  // Code; the console used to ignore the flag and offer them anyway.
  state.exportPresetRequiresLot = Object.fromEntries(
    presets.map((preset) => [preset.id, Boolean(preset.requires_lot_code)]),
  );
  ids.exportPreset.innerHTML = presets
    .map(
      (preset) => `
        <option value="${escapeHtml(preset.id)}">${escapeHtml(preset.label)}</option>
      `,
    )
    .join('');
  ids.exportPreset.value = state.exportPresetDescriptions[selected] ? selected : presets[0]?.id || 'all_records';
  updateExportLink();
}

export async function loadExportPresets() {
  const payload = await api('/api/mock/regengine/export/presets');
  renderExportPresetOptions(payload.presets || []);
}

export function updateExportLink() {
  const csvParams = new URLSearchParams();
  const epcisParams = new URLSearchParams();
  const preset = ids.exportPreset.value || 'all_records';
  const lotCode = ids.exportLot.value.trim();
  const startDate = ids.exportStartDate.value;
  const endDate = ids.exportEndDate.value;
  csvParams.set('preset', preset);
  if (lotCode) {
    csvParams.set('traceability_lot_code', lotCode);
    epcisParams.set('traceability_lot_code', lotCode);
  }
  if (startDate) {
    csvParams.set('start_date', startDate);
    epcisParams.set('start_date', startDate);
  }
  if (endDate) {
    csvParams.set('end_date', endDate);
    epcisParams.set('end_date', endDate);
  }
  const epcisQuery = epcisParams.toString();
  const csvHref = `/api/mock/regengine/export/fda-request?${csvParams.toString()}`;
  ids.exportDownloadLink.href = csvHref;
  ids.epcisDownloadLink.href = `/api/mock/regengine/export/epcis${epcisQuery ? `?${epcisQuery}` : ''}`;
  // The nav "Download FDA export" link was a third, always-unfiltered entry
  // point into the same export; keep it on the same filters and guard.
  if (ids.navExportLink) {
    ids.navExportLink.href = csvHref;
  }
  const presetDescription = state.exportPresetDescriptions[preset] || 'FDA-request CSV export.';
  ids.exportPresetDescription.textContent = `${presetDescription} EPCIS uses the same lot and date filters.`;
  updateExportAvailability();
}

// True when the selected preset needs a Traceability Lot Code the operator
// has not supplied — the combination that used to open a raw JSON 400 page.
export function exportBlockedReason() {
  const preset = ids.exportPreset.value || 'all_records';
  const requiresLot = Boolean(state.exportPresetRequiresLot?.[preset]);
  if (requiresLot && !ids.exportLot.value.trim()) {
    return 'This export preset needs a Traceability Lot Code — enter one above, or pick a different preset.';
  }
  return '';
}

export function updateExportAvailability() {
  const reason = exportBlockedReason();
  if (ids.exportLotHint) {
    ids.exportLotHint.textContent = reason;
    ids.exportLotHint.hidden = !reason;
  }
  // The CSV link is the preset-driven one; EPCIS takes lot/date filters only.
  for (const link of [ids.exportDownloadLink, ids.navExportLink]) {
    if (!link) {
      continue;
    }
    link.classList.toggle('is-blocked', Boolean(reason));
    if (reason) {
      link.setAttribute('aria-disabled', 'true');
    } else {
      link.removeAttribute('aria-disabled');
    }
  }
}

// Exports used to be plain anchors: a 400/404 rendered as raw JSON in a new
// tab, never reached #statusMessage, and the guide rail marked "Export
// evidence" done regardless. Fetching them puts export failures on the same
// error path as every other action, and the guided step is only marked
// complete after a confirmed download.
export async function downloadExport(link, label) {
  const reason = exportBlockedReason();
  if (link !== ids.epcisDownloadLink && reason) {
    setStatus(reason, 'error', 6000);
    flashPanel('.evidence-panel');
    return;
  }
  try {
    const response = await fetch(link.href);
    if (!response.ok) {
      throw new Error(await errorMessageFor(response));
    }
    const blob = await response.blob();
    const filename = exportFilename(response, label);
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
    if (state.events.length) {
      journey.exported = true;
      renderGuide();
    }
    setStatus(`Downloaded ${label} (${filename}).`, 'success', 3500);
  } catch (error) {
    setStatus(`${label} export failed: ${error.message}`, 'error', 7000);
  }
}

export function exportFilename(response, label) {
  const disposition = response.headers.get('content-disposition') || '';
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  if (match) {
    return decodeURIComponent(match[1]);
  }
  return label === 'EPCIS JSON' ? 'epcis-export.json' : 'fda-request-export.csv';
}

export function preferredTraceLot(lotCodes = []) {
  return lotCodes.find((lotCode) => /OUT|TRANSFORM|FC/i.test(lotCode)) || lotCodes.at(-1) || '';
}

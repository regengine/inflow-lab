// The shift log table. It repaints about every 1.5s while the line runs, so
// rows are keyed by record_id and patched cell by cell rather than rebuilt —
// that is what keeps focus and text selection alive on a running line.

import { ids, state } from './dom.js';
import { escapeHtml } from './format.js';
import { deliveryTone } from './labels.js';
import { activeScenarioSummary, recordWarnings } from './audit.js';
import { lookupLineage } from './lineage.js';

// The console must never make an operator read prose to learn whether a gap
// would fail live ingest, so the severity is stamped on the front of the text.
function severityPrefix(warning) {
  return warning.severity === 'required' ? 'Required: ' : 'Recommended: ';
}

let lotClickBound = false;

function bindLotLookupClick() {
  if (lotClickBound || !ids.eventsBody) {
    return;
  }
  ids.eventsBody.addEventListener('click', (event) => {
    const button = event.target.closest('[data-lot]');
    if (!button || !ids.eventsBody.contains(button)) {
      return;
    }
    ids.lotLookup.value = button.dataset.lot || '';
    lookupLineage();
  });
  lotClickBound = true;
}

export function renderEvents(events) {
  bindLotLookupClick();
  const summary = activeScenarioSummary();
  const body = ids.eventsBody;
  if (!events.length) {
    body.innerHTML = `
      <tr>
        <td colspan="9" class="empty-state">No events yet. Load a fixture or run a single batch.</td>
      </tr>
    `;
    state.eventRows = new Map();
    return;
  }

  // The shift log re-renders on every snapshot — about every 1.5s while the
  // line runs. Rebuilding it with innerHTML destroyed the focused lot-code
  // button and any text selection each tick, so rows are keyed by record_id
  // and only the cells whose markup actually changed are rewritten.
  const previousRows = state.eventRows instanceof Map ? state.eventRows : new Map();
  const nextRows = new Map();
  let anchor = body.firstElementChild;

  events.forEach((record) => {
    const event = record.event;
    const audit = recordWarnings(record, summary);
    const warnings = audit.warnings || [];
    // Required-severity warnings are gaps that would fail live RegEngine
    // ingest; recommended ones would not. `recordWarnings` orders required
    // first, so the one warning a row has room for is the worst one.
    const topWarning = warnings[0] || null;
    const warningClass = topWarning?.severity === 'required' ? 'status-blocker' : 'status-warning';
    const cells = [
      escapeHtml(record.sequence_no),
      `<span class="pill">${escapeHtml(event.cte_type)}</span>`,
      `<button class="link-button" type="button" data-lot="${escapeHtml(event.traceability_lot_code)}">${escapeHtml(event.traceability_lot_code)}</button>`,
      escapeHtml(event.product_description),
      escapeHtml(event.location_name),
      escapeHtml(new Date(event.timestamp).toLocaleString()),
      escapeHtml(record.destination_mode),
      `${escapeHtml(record.delivery_attempts || 0)}
        ${topWarning ? `<small class="${escapeHtml(warningClass)}">${escapeHtml(severityPrefix(topWarning))}${escapeHtml(topWarning.message)}</small>` : ''}
        ${!audit.evaluated ? `<small class="status-unevaluated">Audit not evaluated</small>` : ''}`,
      `<span class="status-pill" data-tone="${escapeHtml(deliveryTone(record.delivery_status))}">${escapeHtml(record.delivery_status)}</span>
        ${record.error ? `<small class="status-error">${escapeHtml(record.error)}</small>` : ''}`,
    ];
    const rowClass = [
      warnings.length ? ((audit.requiredCount || 0) ? 'has-audit-blocker' : 'has-audit-warning') : '',
      audit.evaluated ? '' : 'audit-not-evaluated',
    ]
      .filter(Boolean)
      .join(' ');
    const key = record.record_id || `seq-${record.sequence_no}`;
    let entry = previousRows.get(key);
    if (!entry) {
      const row = document.createElement('tr');
      cells.forEach((cellHtml) => {
        const cell = document.createElement('td');
        cell.innerHTML = cellHtml;
        row.appendChild(cell);
      });
      entry = { row, cells };
    } else {
      const row = entry.row;
      // Rewrite only the cells that changed; an untouched cell keeps the DOM
      // node the operator may be focused in or selecting text from.
      cells.forEach((cellHtml, index) => {
        if (entry.cells[index] !== cellHtml) {
          const cell = row.children[index];
          if (cell) {
            cell.innerHTML = cellHtml;
          }
        }
      });
      entry.cells = cells;
    }
    if (entry.row.className !== rowClass) {
      entry.row.className = rowClass;
    }
    // Position without moving rows that are already in the right place —
    // re-inserting a node blurs anything focused inside it.
    if (anchor === entry.row) {
      anchor = anchor.nextElementSibling;
    } else {
      body.insertBefore(entry.row, anchor);
    }
    nextRows.set(key, entry);
  });

  while (anchor) {
    const next = anchor.nextElementSibling;
    anchor.remove();
    anchor = next;
  }
  state.eventRows = nextRows;
}

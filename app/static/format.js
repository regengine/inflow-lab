// Escaping and value formatting. escapeHtml() is what makes every `${...}`
// inside a quoted HTML attribute safe, so it has no dependencies of its own
// and can be imported from anywhere.

export function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function cteLabel(cteType) {
  return String(cteType || 'event').replaceAll('_', ' ');
}

export function formatDateTime(value) {
  return escapeHtml(new Date(value).toLocaleString());
}

export function formatKdeValue(value) {
  if (Array.isArray(value)) {
    return value.join(', ');
  }
  if (value && typeof value === 'object') {
    return JSON.stringify(value);
  }
  return value ?? '';
}

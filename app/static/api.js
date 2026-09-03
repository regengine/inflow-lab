// The fetch wrapper and the error-message flattening behind it. Nothing in
// here touches the DOM, so an error string is produced the same way for a
// button handler, a stream reconnect, or a background refresh.

// FastAPI answers a validation failure with `detail` as a *list of objects*.
// Interpolating that into an Error stringifies it to "[object Object]" and
// throws away the only actionable half of the response, so flatten it into
// "field: message" sentences first. String details pass through unchanged.
export function formatErrorDetail(detail) {
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') {
          return item;
        }
        if (!item || typeof item !== 'object') {
          return '';
        }
        const location = Array.isArray(item.loc)
          ? item.loc.filter((part) => part !== 'body').at(-1)
          : null;
        const message = typeof item.msg === 'string' ? item.msg : '';
        if (!message) {
          return '';
        }
        return location ? `${location}: ${message}` : message;
      })
      .filter(Boolean);
    if (messages.length) {
      return messages.join('; ');
    }
  }
  if (detail && typeof detail === 'object') {
    const message = typeof detail.msg === 'string' ? detail.msg : '';
    if (message) {
      return message;
    }
  }
  return '';
}

// Error bodies are not always JSON — a proxy 502 is usually an HTML page, and
// swallowing it left the operator with a bare status code. Fall back to the
// HTTP status plus a short snippet of whatever text came back.
export async function errorMessageFor(response) {
  const raw = await response.text().catch(() => '');
  let detail = '';
  if (raw) {
    try {
      detail = formatErrorDetail(JSON.parse(raw).detail);
    } catch (error) {
      detail = '';
    }
  }
  const statusLine = `Request failed: ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`;
  if (detail) {
    return `${statusLine} — ${detail}`;
  }
  const snippet = raw.replace(/\s+/g, ' ').trim().slice(0, 160);
  return snippet ? `${statusLine} — ${snippet}` : statusLine;
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await errorMessageFor(response));
  }
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return response.json();
  }
  return response.text();
}

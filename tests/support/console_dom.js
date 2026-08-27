'use strict';
//
// A deliberately small DOM stand-in for exercising app/static/app.js from
// pytest (see tests/test_console_behavior.py).
//
// This repo has no JS test runner and CI installs no npm packages, so the
// console's browser behaviour was previously untestable -- issues #148-#151
// and #193-#196 are all defects in that layer. Rather than assert on the
// *text* of app.js (a test that cannot actually detect the bug), this shim
// implements just enough of the DOM for app.js to load and run under node:
// element attributes/dataset/classList, an innerHTML setter that reparses
// into queryable children, attribute/class/id selector matching, focus
// tracking via document.activeElement, and stubs for fetch/EventSource/
// localStorage.
//
// index.html is parsed for real so elements start with the same tags,
// attributes and default <select> values the browser would give them --
// #148 is precisely a bug about those raw HTML defaults.
//
// Usage: node console_dom.js <repo-root>  with the test snippet on stdin.
// The snippet runs in app.js's own global lexical scope, so it can call
// app.js functions and read `state`/`ids`/`journey` directly. Whatever it
// returns is printed as JSON after a __RESULT__ marker.
//
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = process.argv[2];
if (!repoRoot) {
  throw new Error('usage: node console_dom.js <repo-root>');
}

const VOID_TAGS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr',
]);

function decodeEntities(text) {
  return String(text)
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&nbsp;', ' ')
    .replaceAll('&amp;', '&');
}

function camelCase(name) {
  return name.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

class ClassList {
  constructor(element) {
    this._element = element;
  }
  _tokens() {
    return String(this._element.attributes.class || '').split(/\s+/).filter(Boolean);
  }
  _write(tokens) {
    this._element.attributes.class = tokens.join(' ');
  }
  add(...names) {
    const tokens = this._tokens();
    for (const name of names) {
      if (name && !tokens.includes(name)) {
        tokens.push(name);
      }
    }
    this._write(tokens);
  }
  remove(...names) {
    this._write(this._tokens().filter((token) => !names.includes(token)));
  }
  contains(name) {
    return this._tokens().includes(name);
  }
  get value() {
    return this._tokens().join(' ');
  }
}

let nodeSeq = 0;
// Every programmatic .click(), so a test can see that a blob download was
// actually triggered rather than only that no error was raised.
const clickLog = [];

class El {
  constructor(tagName = 'div', attributes = {}) {
    this.tagName = String(tagName).toUpperCase();
    this.attributes = {};
    this.dataset = {};
    this.childNodes = [];
    this.parentNode = null;
    this.classList = new ClassList(this);
    this._listeners = Object.create(null);
    this._innerHTML = '';
    this._text = '';
    this.checked = false;
    this.disabled = false;
    this.files = [];
    this.offsetWidth = 1;
    this.selected = false;
    this._nodeId = ++nodeSeq;
    this.value = '';
    for (const [name, raw] of Object.entries(attributes)) {
      this.setAttribute(name, raw);
    }
    if (!('value' in attributes)) {
      this.value = '';
    }
  }

  get id() {
    return this.attributes.id || '';
  }

  get hidden() {
    return 'hidden' in this.attributes;
  }
  set hidden(flag) {
    if (flag) {
      this.attributes.hidden = '';
    } else {
      delete this.attributes.hidden;
    }
  }

  get href() {
    return this.attributes.href || '';
  }
  set href(value) {
    this.attributes.href = value === undefined || value === null ? '' : String(value);
  }

  get inert() {
    return 'inert' in this.attributes;
  }
  set inert(flag) {
    if (flag) {
      this.attributes.inert = '';
    } else {
      delete this.attributes.inert;
    }
  }

  setAttribute(name, value) {
    const raw = value === undefined || value === null ? '' : String(value);
    this.attributes[name] = raw;
    if (name.startsWith('data-')) {
      this.dataset[camelCase(name.slice(5))] = raw;
    }
    if (name === 'value') {
      this.value = raw;
    }
  }
  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }
  hasAttribute(name) {
    return name in this.attributes;
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name.startsWith('data-')) {
      delete this.dataset[camelCase(name.slice(5))];
    }
  }

  get children() {
    return this.childNodes.slice();
  }

  get textContent() {
    if (this.childNodes.length) {
      return this.childNodes.map((child) => child.textContent).join('');
    }
    return this._text;
  }
  set textContent(value) {
    this._detachSubtree();
    this.childNodes = [];
    this._innerHTML = '';
    this._text = value === undefined || value === null ? '' : String(value);
  }

  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(html) {
    this._detachSubtree();
    this._text = '';
    this._innerHTML = html === undefined || html === null ? '' : String(html);
    this.childNodes = parseFragment(this._innerHTML, this);
  }

  _detachSubtree() {
    // Replacing markup destroys the old nodes; a browser drops focus back to
    // <body> when the focused element is one of them. #195 is exactly that
    // behaviour, so the shim has to reproduce it.
    for (const node of descendants(this)) {
      if (documentStub.activeElement === node) {
        documentStub.activeElement = documentStub.body;
      }
      node.parentNode = null;
    }
  }

  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  removeEventListener(type, handler) {
    const bucket = this._listeners[type];
    if (bucket) {
      this._listeners[type] = bucket.filter((fn) => fn !== handler);
    }
  }
  listenerCount(type) {
    return (this._listeners[type] || []).length;
  }
  dispatchEvent(type, event = {}) {
    const evt = { type, target: event.target || this, ...event };
    evt.currentTarget = this;
    if (!evt.preventDefault) {
      evt.preventDefault = () => {
        evt.defaultPrevented = true;
      };
    }
    const results = (this._listeners[type] || []).map((fn) => fn(evt));
    return Promise.all(results).then(() => evt);
  }

  focus() {
    documentStub.activeElement = this;
  }
  blur() {
    if (documentStub.activeElement === this) {
      documentStub.activeElement = documentStub.body;
    }
  }
  scrollIntoView() {}
  appendChild(child) {
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  removeChild(child) {
    this.childNodes = this.childNodes.filter((node) => node !== child);
    child.parentNode = null;
    return child;
  }
  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }
  click() {
    clickLog.push({ tagName: this.tagName, href: this.href, download: this.download || '' });
    return this.dispatchEvent('click');
  }

  querySelectorAll(selector) {
    return descendants(this).filter((node) => matches(node, selector));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (matches(node, selector)) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }
}

function descendants(root) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes) {
      out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

// --- selector matching --------------------------------------------------
// Supports the shapes app.js actually uses: `#id`, `.class`, `tag`,
// `[attr]`, `[attr="value"]`, comma lists, and descendant combinators
// (only the right-most compound is checked, which is enough here).
function matches(node, selector) {
  return String(selector)
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .some((part) => matchesCompound(node, part.split(/\s+/).pop()));
}

function matchesCompound(node, compound) {
  const tokens = compound.match(/\[[^\]]*\]|[.#]?[\w-]+/g) || [];
  return tokens.every((token) => {
    if (token.startsWith('[')) {
      const body = token.slice(1, -1);
      const eq = body.indexOf('=');
      if (eq === -1) {
        return node.hasAttribute(body);
      }
      const name = body.slice(0, eq);
      const wanted = body.slice(eq + 1).replace(/^["']|["']$/g, '');
      return node.getAttribute(name) === wanted;
    }
    if (token.startsWith('.')) {
      return node.classList.contains(token.slice(1));
    }
    if (token.startsWith('#')) {
      return node.id === token.slice(1);
    }
    return node.tagName === token.toUpperCase();
  });
}

// --- markup parsing -----------------------------------------------------
const TAG_RE = /<(\/?)([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>])*?)\/?>/g;
const ATTR_RE = /([a-zA-Z_:][-\w:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

function parseAttributes(raw) {
  const attrs = {};
  let match;
  ATTR_RE.lastIndex = 0;
  while ((match = ATTR_RE.exec(raw)) !== null) {
    const name = match[1];
    const value = match[2] ?? match[3] ?? match[4] ?? '';
    attrs[name] = decodeEntities(value);
  }
  return attrs;
}

function parseFragment(html, root, registry = null) {
  const source = String(html).replace(/<!--[\s\S]*?-->/g, '');
  const created = [];
  const stack = [root];
  let cursor = 0;
  let match;
  TAG_RE.lastIndex = 0;
  while ((match = TAG_RE.exec(source)) !== null) {
    const text = source.slice(cursor, match.index);
    cursor = TAG_RE.lastIndex;
    const parent = stack[stack.length - 1];
    if (text.trim() && parent !== root) {
      parent._text += decodeEntities(text);
    }
    const [full, closing, tagName, rawAttrs] = match;
    const lower = tagName.toLowerCase();
    if (closing) {
      for (let i = stack.length - 1; i > 0; i -= 1) {
        if (stack[i].tagName === tagName.toUpperCase()) {
          stack.length = i;
          break;
        }
      }
      continue;
    }
    const element = new El(tagName, parseAttributes(rawAttrs));
    element.parentNode = parent;
    parent.childNodes.push(element);
    if (parent === root) {
      created.push(element);
    }
    if (registry && element.id && !registry.has(element.id)) {
      registry.set(element.id, element);
    }
    if (!VOID_TAGS.has(lower) && !full.endsWith('/>')) {
      stack.push(element);
    }
  }
  const tail = source.slice(cursor);
  if (tail.trim() && stack[stack.length - 1] !== root) {
    stack[stack.length - 1]._text += decodeEntities(tail);
  }
  return created;
}

// A <select> reports the selected <option>'s value (or the first option's).
// #148 is about exactly those markup defaults, so seed them faithfully.
function seedFormDefaults(root) {
  for (const node of descendants(root)) {
    if (node.tagName === 'SELECT') {
      const options = node.querySelectorAll('option');
      const chosen = options.find((option) => option.hasAttribute('selected')) || options[0];
      node.value = chosen ? chosen.getAttribute('value') || '' : '';
    } else if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') {
      node.value = node.getAttribute('value') || '';
    }
  }
}

// --- document / window --------------------------------------------------
const registry = new Map();
const documentStub = {
  body: new El('body'),
  activeElement: null,
  _listeners: Object.create(null),
  getElementById(id) {
    if (!registry.has(id)) {
      // Never hand app.js a null: a missing id in this shim should surface
      // as an assertion about behaviour, not a TypeError at load time.
      const orphan = new El('div', { id });
      registry.set(id, orphan);
    }
    return registry.get(id);
  },
  createElement(tagName) {
    return new El(tagName);
  },
  querySelectorAll(selector) {
    return descendants(documentStub.body).filter((node) => matches(node, selector));
  },
  querySelector(selector) {
    return documentStub.querySelectorAll(selector)[0] || null;
  },
  addEventListener(type, handler) {
    (documentStub._listeners[type] || (documentStub._listeners[type] = [])).push(handler);
  },
  removeEventListener(type, handler) {
    const bucket = documentStub._listeners[type];
    if (bucket) {
      documentStub._listeners[type] = bucket.filter((fn) => fn !== handler);
    }
  },
  dispatchEvent(type, event = {}) {
    const evt = { type, target: event.target || documentStub.body, ...event };
    if (!evt.preventDefault) {
      evt.preventDefault = () => {
        evt.defaultPrevented = true;
      };
    }
    const results = (documentStub._listeners[type] || []).map((fn) => fn(evt));
    return Promise.all(results).then(() => evt);
  },
};

const indexHtml = fs.readFileSync(path.join(repoRoot, 'app', 'static', 'index.html'), 'utf8');
parseFragment(indexHtml, documentStub.body, registry);
seedFormDefaults(documentStub.body);
documentStub.activeElement = documentStub.body;

const storage = new Map();
const localStorageStub = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
  clear: () => storage.clear(),
};

class EventSourceStub {
  constructor(url) {
    this.url = url;
    this._listeners = Object.create(null);
    EventSourceStub.instances.push(this);
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  emit(type, event) {
    return Promise.all((this._listeners[type] || []).map((fn) => fn(event)));
  }
  close() {
    this.closed = true;
  }
}
EventSourceStub.instances = [];

function makeResponse({ status = 200, body = null, contentType = 'application/json', text = null } = {}) {
  const payload = text === null ? JSON.stringify(body) : text;
  return {
    __response: true,
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 422 ? 'Unprocessable Entity' : status === 502 ? 'Bad Gateway' : 'OK',
    headers: { get: (name) => (String(name).toLowerCase() === 'content-type' ? contentType : null) },
    json: async () => {
      if (contentType.includes('json')) {
        return JSON.parse(payload);
      }
      throw new SyntaxError('Unexpected token in JSON');
    },
    text: async () => payload,
    blob: async () => ({ __blob: true, type: contentType, size: payload.length, text: async () => payload }),
  };
}

// Default: never settles. Keeps app.js's bootstrap fetches pending so a test
// starts from untouched state and no fallback-poll timer is created.
let fetchHandler = () => new Promise(() => {});

const sandbox = {
  document: documentStub,
  fetch: (input, options) => Promise.resolve(fetchHandler(String(input), options || {})),
  EventSource: EventSourceStub,
  navigator: { userAgent: 'console-dom-shim' },
  location: { href: 'http://localhost/', origin: 'http://localhost' },
};
sandbox.window = sandbox;
sandbox.window.localStorage = localStorageStub;
sandbox.self = sandbox;

for (const [key, value] of Object.entries(sandbox)) {
  // node predefines some of these (navigator, location) as getter-only
  // accessors on globalThis, so assign through defineProperty.
  Object.defineProperty(globalThis, key, { value, writable: true, configurable: true });
}
// node has URL.createObjectURL, but nothing to observe with it; record the
// object URLs app.js mints so a download is assertable.
const objectUrls = [];
const realCreateObjectURL = URL.createObjectURL;
URL.createObjectURL = (blob) => {
  const handle = `blob:console-dom/${objectUrls.length}`;
  objectUrls.push({ handle, blob, revoked: false });
  return handle;
};
URL.revokeObjectURL = (handle) => {
  const entry = objectUrls.find((item) => item.handle === handle);
  if (entry) {
    entry.revoked = true;
  }
};
void realCreateObjectURL;

globalThis.__dom = {
  El,
  clickLog,
  objectUrls,
  documentStub,
  makeResponse,
  descendants,
  matches,
  localStorageStub,
  EventSourceStub,
  setFetch: (handler) => {
    fetchHandler = handler;
  },
};

// Convenience for tests: route table keyed by exact path first, then by
// longest matching prefix. Values are plain bodies (200/JSON) or the result
// of __dom.makeResponse() for anything else.
globalThis.__dom.routes = (table) => {
  const keys = Object.keys(table).sort((a, b) => b.length - a.length);
  globalThis.__dom.setFetch((url, options) => {
    const key = keys.find((candidate) => url === candidate) ||
      keys.find((candidate) => url.startsWith(candidate));
    if (key === undefined) {
      throw new Error(`unrouted fetch: ${url}`);
    }
    const value = table[key];
    const resolved = typeof value === 'function' ? value(url, options) : value;
    return Promise.resolve(resolved).then((body) =>
      body && body.__response ? body : makeResponse({ body }),
    );
  });
};

// The three payloads refresh() fetches, in the shape the real API returns.
globalThis.__dom.snapshotRoutes = (overrides = {}) => ({
  '/api/health': {
    status: 'ok',
    tenant: 'local-demo',
    build: { version: '0.1.0', commit_sha_short: 'abc1234' },
    auth: { enabled: false, uses_default_storage: true },
  },
  '/api/simulate/status': {
    running: false,
    config: {
      source: 'codex-simulator',
      scenario: 'leafy_greens_supplier',
      scale: 'midsize',
      interval_seconds: 1.5,
      batch_size: 3,
      seed: null,
      persist_path: 'data/events.jsonl',
      delivery: { mode: 'mock', endpoint: null, api_key: null, tenant_id: null, mock_friction: [] },
    },
    stats: { total_records: 0, unique_lots: 0, delivery: {}, engine: {}, audit: null },
  },
  '/api/events?limit=100': { events: [] },
  ...overrides,
});

const appSource = fs.readFileSync(path.join(repoRoot, 'app', 'static', 'app.js'), 'utf8');
vm.runInThisContext(appSource, { filename: 'app/static/app.js' });

const snippet = fs.readFileSync(0, 'utf8');
vm
  .runInThisContext(`(async () => {\n${snippet}\n})()`, { filename: 'snippet.js' })
  .then((value) => {
    process.stdout.write(`__RESULT__${JSON.stringify(value === undefined ? null : value)}`);
    process.exit(0);
  })
  .catch((error) => {
    process.stderr.write(String((error && error.stack) || error));
    process.exit(1);
  });

# Security Boundaries

Inflow Lab is a simulator. Its security boundary is designed for safe demos, test runs, and controlled live-ingest trials.

## Data Boundary

- Simulated events are not customer source-of-record data.
- Local event logs are demo/test artifacts.
- Tenant-scoped simulator data must remain isolated under the configured tenant storage path.
- Tenant isolation does not depend on Basic Auth. `X-RegEngine-Tenant` selects a tenant's own controller and storage whether or not credentials are configured; a request that sends no header gets the shared `local-demo` store, which is the documented default for a local demo and not a tenant. Auth changes who may *ask* for a tenant, not whether the tenants are separated. (The number of tenants one process will materialize is bounded either way — see the Authentication Boundary.)
- Tenant storage is reachable only by naming a tenant, never by naming a path. A caller-supplied `persist_path` (accepted only for the unauthenticated local demo) is confined to the data root *and* refused if it resolves inside the tenant storage root — otherwise `data/tenants/<other>/events.jsonl` would read another tenant's log through the exports and, since a reset unlinks its persist path, delete it.
- Reset and delete operations must not affect other tenant scopes. A tenant delete resolves its target and refuses to recurse into anything that is not a directory *inside* the tenant storage root, so the recursive delete is bounded by construction rather than by the tenant-id regex alone.
- Retained history is bounded on disk as well as in memory. One retention bound (`REGENGINE_STORE_MAX_HISTORY`, default `50000`) governs both: records leave the live event log when they leave the in-memory history, and they leave by being appended to a `.1` archive beside it rather than deleted. The store therefore cannot hold a log it no longer fully represents, and disk growth is bounded for the same reason memory is. Reads are served from memory; the log stays the durable record and is what a restart reloads from.

## Authentication Boundary

- Basic Auth is optional for local mock demos.
- Shared-demo or remote deployments must enable Basic Auth.
- A deployment that requires Basic Auth must fail closed rather than serve open. `REGENGINE_REQUIRE_AUTH` enforces this at startup: when it is set to a truthy value and `REGENGINE_BASIC_AUTH_USERNAME`/`REGENGINE_BASIC_AUTH_PASSWORD` are not both configured, the process refuses to start. The shipped container image sets `REGENGINE_REQUIRE_AUTH=1`, so a deploy that loses its credential variables goes down loudly instead of exposing every state-changing endpoint. `REGENGINE_REQUIRE_AUTH=0` is the deliberate opt-out, for local loopback demos only.
- Basic Auth username and password are compared without short-circuiting, so a wrong username and a wrong password cost the same time.
- Browser-origin state-changing requests must come from trusted origins when credentials are enabled.
- The number of tenant scopes one process will materialize is bounded by `REGENGINE_MAX_TENANTS` (default `100`), with or without Basic Auth, so an unauthenticated caller cycling `X-RegEngine-Tenant` cannot grow memory and disk without limit. Requests that would exceed the cap are refused with `429`.
- Health endpoints may expose non-secret build and status metadata only.

## Delivery Boundary

- `mock` mode is the default and safest mode.
- `none` mode generates and persists events locally without delivery.
- `live` mode sends real traffic to RegEngine and must require explicit operator configuration.
- Live trial scripts must perform a mock dry run before live delivery.
- A live delivery endpoint must be an allowed egress destination, checked *before* any credential header is built. Non-`http(s)` schemes are refused, as are loopback, private, link-local, reserved, and cloud-metadata hosts — including a public hostname that resolves to one of them. The same check gates the connection-test probe, which returns a `blocked_endpoint` verdict instead of sending anything.
- `REGENGINE_ALLOWED_DELIVERY_HOSTS` is an optional strict allowlist (comma-separated; a leading dot matches subdomains, so `.regengine.co` allows `www.regengine.co`). When set, every host outside the list is refused, including hosts the private-host opt-out would otherwise permit.
- `REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS=1` is the only way to target loopback/private hosts, and exists for pointing the simulator at a local RegEngine stack (`scripts/customer_journey.py --local`). It must stay unset on shared or deployed profiles.
- `REGENGINE_DELIVERY_DNS_GUARD=0` skips only the DNS resolution check, for sealed environments with no resolver. Scheme, allowlist, hostname, and literal-address checks still apply.

## Secret Boundary

- RegEngine API keys, Basic Auth passwords, and live delivery credentials must not be logged, returned in API payloads, or displayed in the dashboard.
- No credential may be placed in a request header for an endpoint that has not passed the delivery-endpoint check above; a blocked endpoint fails before the header is built, so a redirected or attacker-supplied endpoint cannot collect the API key.
- Stored credentials are scoped to an **origin**, not to a host. `POST /api/integration/test` accepts a caller-supplied endpoint, and it inherits the saved API key and tenant id only when `(scheme, host, port)` matches the configured endpoint exactly — with the scheme's default port made explicit, so `https://host` and `https://host:443` are one origin. Comparing bare hostnames was not enough: an `http://` spelling of the configured host sent the key in cleartext, a different port redirected it to whatever else listens on that host, and embedded userinfo attached the caller's own credentials to the probe. Each of those is a different security context, so each is refused exactly the way a different host is — the stored credentials are withheld and no request is made.
- A test that withholds stored credentials says so. It returns the `credentials_withheld` verdict naming the configured origin and the probed one, not the generic `not_configured` (which stays reserved for its own case: no credentials configured at all). A verdict that names a condition which did not fail sends the operator to re-enter a key that was already correct.
- Remote smoke tooling must redact configured credentials in failure output, and must constrain its base URL to an allowlisted host before attaching Basic Auth.

## Evidence Boundary

Inflow Lab can demonstrate the shape of FSMA evidence, but production evidence belongs in RegEngine.

- Exported evidence artifacts are inert. Every cell of the FDA-request CSV is caller-influenced (product descriptions, location names, `reference_document_*` KDEs), and the file exists to be opened in a spreadsheet by a human reviewing FSMA evidence — so a value that begins with `=`, `+`, `-`, `@`, a tab, or a carriage return is written prefixed with an apostrophe and opens as text rather than as a live formula. The neutralization is applied to every column on the way out, so a column added later is covered without being remembered.

```text
simulated event != production evidence
```


# Security Boundaries

Inflow Lab is a simulator. Its security boundary is designed for safe demos, test runs, and controlled live-ingest trials.

## Data Boundary

- Simulated events are not customer source-of-record data.
- Local event logs are demo/test artifacts.
- Tenant-scoped simulator data must remain isolated under the configured tenant storage path.
- Reset and delete operations must not affect other tenant scopes.

**Tenant isolation requires Basic Auth.** With `REGENGINE_BASIC_AUTH_USERNAME`
and `REGENGINE_BASIC_AUTH_PASSWORD` unset, `TenantContext.uses_default_storage`
is true for every request, so the `X-RegEngine-Tenant` header is accepted and
then ignored: all requests share the single `local-demo` store. This is
deliberate rather than an oversight — routing by an unauthenticated header would
let any caller mint tenant directories without limit — but it does mean the
isolation guarantee above holds **only when auth is enabled**. Since every
non-loopback deployment must enable Basic Auth (see the Authentication Boundary
below), the guarantee holds wherever it matters; a local no-auth run is
single-tenant and should be treated as such.

- The persisted event log is append-only and unbounded on disk, while the
  in-memory ring is capped (`EventStore.max_records`, default 5000). Reads that
  need the whole log go to disk, so they stay complete as it grows; `recent()`
  is the ring and is bounded by design.
- **Single-worker is a hard requirement.** Run state, the event ring and the
  store's write lock all live in one process's memory, and the lock is
  per-process, so two uvicorn workers would interleave appends and file
  rewrites on the same JSONL with no coordination. Do not set `--workers` above
  1 or run more than one replica against the same data directory.

## Authentication Boundary

- Basic Auth is optional for local mock demos.
- Shared-demo or remote deployments must enable Basic Auth.
- Browser-origin state-changing requests must come from trusted origins when credentials are enabled.
- Health endpoints may expose non-secret build and status metadata only.

## Delivery Boundary

- `mock` mode is the default and safest mode.
- `none` mode generates and persists events locally without delivery.
- `live` mode sends real traffic to RegEngine and must require explicit operator configuration.
- Live trial scripts must perform a mock dry run before live delivery.

## Secret Boundary

- RegEngine API keys, Basic Auth passwords, and live delivery credentials must not be logged, returned in API payloads, or displayed in the dashboard.
- Remote smoke tooling must redact configured credentials in failure output.

## Evidence Boundary

Inflow Lab can demonstrate the shape of FSMA evidence, but production evidence belongs in RegEngine.

```text
simulated event != production evidence
```


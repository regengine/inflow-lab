# Security Boundaries

Inflow Lab is a simulator. Its security boundary is designed for safe demos, test runs, and controlled live-ingest trials.

## Data Boundary

- Simulated events are not customer source-of-record data.
- Local event logs are demo/test artifacts.
- Tenant-scoped simulator data must remain isolated under the configured tenant storage path.
- Reset and delete operations must not affect other tenant scopes.

## Authentication Boundary

- Basic Auth is optional for local mock demos.
- Shared-demo or remote deployments must enable Basic Auth.
- Browser-origin state-changing requests must come from trusted origins when credentials are enabled.
- Health endpoints may expose non-secret build and status metadata only.
- Concurrently active tenant controllers are capped (`REGENGINE_MAX_TENANT_CONTROLLERS`, default 50). Choosing a tenant is a single unauthenticated header, and honoring one commits memory and disk, so the ceiling is enforced whether or not Basic Auth is on. The shared `local-demo` tenant is exempt. Freeing a slot requires an authenticated operator reset/delete or a process restart; there is no idle eviction.

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


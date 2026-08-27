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
- A `live` delivery endpoint is resolved and checked immediately before every outbound request, and endpoints that resolve to loopback, private, link-local, reserved, unspecified, multicast or cloud-metadata addresses are refused, so an authenticated operator cannot aim the configured API key and tenant id at the host itself, its LAN, or its cloud control plane. `REGENGINE_ALLOW_PRIVATE_ENDPOINTS=1` disables the check for local development only.
- That check resolves the endpoint host exactly once and the request is dialed at the address it validated, carrying the original hostname through as the `Host` header and the TLS SNI/verification name. Certificate verification is unchanged and still applies to the configured hostname, never to the address. This is what stops a hostile DNS zone from answering the check with a public address and the connection with `127.0.0.1`.
- The pin is skipped, and the endpoint dialed by hostname as before, whenever an HTTP proxy would carry the request (either the endpoint URL or its resolved-address form). With a proxy the socket is opened by the proxy, so pinning cannot be honored and would break certificate verification. In a fully proxied deployment nothing on this side resolves at all; in a deployment that proxies everything *except* the endpoint's hostname, the request goes direct and the rebinding gap remains open for it — exempt the resolved address from `NO_PROXY` as well, or remove the proxy for that host.

## Secret Boundary

- RegEngine API keys, Basic Auth passwords, and live delivery credentials must not be logged, returned in API payloads, or displayed in the dashboard.
- Remote smoke tooling must redact configured credentials in failure output.
- Remote smoke tooling must only send those credentials to an allowlisted host. `scripts/remote_smoke.py` and `scripts/browser_smoke.py` fail closed on any other `base_url` before a credential-carrying config is built, so a `workflow_dispatch` input cannot redirect the shared-demo Basic Auth secrets to an attacker-controlled server. Loopback is exempt (local runs); plaintext `http://` to a non-loopback host is refused because Basic Auth would be on the wire. Override with `REGENGINE_REMOTE_ALLOWED_HOSTS` (space/comma separated) for a different deployment - it is an environment variable, not a workflow input, so changing it takes a reviewed commit.

## Evidence Boundary

Inflow Lab can demonstrate the shape of FSMA evidence, but production evidence belongs in RegEngine.

```text
simulated event != production evidence
```


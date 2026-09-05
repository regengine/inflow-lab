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
- A cleartext (`http://`) endpoint is refused as well, because every live delivery and every connection probe carries the RegEngine API key in a request header. `REGENGINE_ALLOW_CLEARTEXT_DELIVERY=1` relaxes the scheme check only, for a trusted network where TLS terminates elsewhere; the address checks above still apply.
- That check resolves the endpoint host exactly once and the request is dialed at the address it validated, carrying the original hostname through as the `Host` header and the TLS SNI/verification name. Certificate verification is unchanged and still applies to the configured hostname, never to the address. This is what stops a hostile DNS zone from answering the check with a public address and the connection with `127.0.0.1`.
- The pin is skipped, and the endpoint dialed by hostname as before, whenever an HTTP proxy would carry the request for the configured hostname. With a proxy the socket is opened by the proxy, so pinning cannot be honored and would break certificate verification; in that deployment nothing on this side resolves at all, so there is no second lookup to poison. In a deployment that proxies everything *except* the endpoint's hostname (`NO_PROXY` names the host but not its address), the client mounts an explicit direct transport for the validated address, so the connection honors the hostname's exemption and the pin still applies. The direct transport is httpx's own default, with hostname verification on.
- The connection test (`POST /api/integration/test`) reads RegEngine's `/health` once and compares two advertised facts against this side: the ingest contract version, and whether RegEngine verifies webhook HMAC signatures (`webhook_hmac_configured`). A disagreement is reported as `contract_mismatch` or `signature_mismatch` rather than `connected`, because either one means every live ingest would fail or go unverified while the read probe stays green. When RegEngine does not advertise a fact, no verdict is inferred from its absence.

## Secret Boundary

- RegEngine API keys, Basic Auth passwords, and live delivery credentials must not be logged, returned in API payloads, or displayed in the dashboard.
- Remote smoke tooling must redact configured credentials in failure output.

## Evidence Boundary

Inflow Lab can demonstrate the shape of FSMA evidence, but production evidence belongs in RegEngine.

```text
simulated event != production evidence
```


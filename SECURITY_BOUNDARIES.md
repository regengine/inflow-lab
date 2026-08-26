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
- Remote smoke tooling must redact configured credentials in failure output, and must constrain its base URL to an allowlisted host before attaching Basic Auth.

## Evidence Boundary

Inflow Lab can demonstrate the shape of FSMA evidence, but production evidence belongs in RegEngine.

```text
simulated event != production evidence
```


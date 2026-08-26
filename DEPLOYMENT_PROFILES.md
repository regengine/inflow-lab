# Deployment Profiles

This guide gives concrete run profiles for local development, shared design-partner demos, and live-ingest trials. All profiles preserve mock mode as the default unless live delivery is explicitly configured per request.

## Profile Matrix

| Profile | Bind address | Auth | Storage | Delivery default | Best for |
|---|---|---|---|---|---|
| Local demo | `127.0.0.1` | Off | `data/events.jsonl` | `mock` | Solo development and screen-share demos |
| Shared demo | `0.0.0.0` behind TLS/proxy | Basic Auth on | `data/tenants/{tenant_id}/` | `mock` | Design partners, multiple tenants, non-live workshops |
| Live ingest trial | Prefer private host or VPN | Basic Auth on | Tenant-scoped | `mock`; switch request to `live` | Controlled RegEngine workspace validation |

## Single-process requirement

Every profile below runs the simulator as **one process with one worker and one replica**. This is a hard requirement, not a default worth tuning.

Simulation run state (whether a run loop is active, and the per-tenant controller registry) lives in a single process's memory with no shared coordination point. With two or more workers each request lands on an arbitrary process, so a Stop request can return `200` from a process whose loop was already idle while a *different* process keeps generating and delivering events — including live RegEngine traffic.

The app enforces this at startup: if any of `WEB_CONCURRENCY`, `UVICORN_WORKERS`, `GUNICORN_WORKERS`, `RAILWAY_REPLICA_COUNT`, or `WEB_REPLICAS` is set above `1`, startup fails with a `MultiProcessRuntimeError` naming the offending variable rather than booting into a state where Stop silently does nothing. Leaving these unset is the supported configuration.

Do not add `--workers` to the `uvicorn` command, and do not scale Railway replicas above 1. If the demo needs more throughput, raise `batch_size`/lower `interval_seconds` on a single process, or run separate instances against separate data roots.

## Common Prerequisites

```bash
python3 -m pip install --upgrade uv
uv sync --group dev
uv run pytest
uv run python scripts/smoke_regression.py
```

Before exposing any profile to another person, verify:

```bash
curl http://127.0.0.1:8000/api/healthz
curl http://127.0.0.1:8000/api/health
```

## Local Demo Profile

Use this profile for development and screen-share demos on one machine.

```bash
unset REGENGINE_BASIC_AUTH_USERNAME
unset REGENGINE_BASIC_AUTH_PASSWORD
unset REGENGINE_DEFAULT_TENANT
export REGENGINE_REQUIRE_AUTH=0
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`REGENGINE_REQUIRE_AUTH=0` is the explicit opt-out from the fail-closed auth
check: when `REGENGINE_REQUIRE_AUTH` is truthy and the Basic Auth username and
password are not both set, startup fails instead of serving open. Leaving the
variable unset works the same way for a local shell, but setting it to `0` keeps
this profile correct in an environment that inherits `REGENGINE_REQUIRE_AUTH=1`
(the container image sets it).

Expected health context:

- `tenant`: `local-demo`
- `auth.enabled`: `false`
- `auth.uses_default_storage`: `true`
- `status.config.delivery.mode`: `mock`

Quick setup for a repeatable fixture demo:

```bash
curl -X POST http://127.0.0.1:8000/api/demo-fixtures/fresh_cut_transformation/load \
  -H 'Content-Type: application/json' \
  -d '{"reset":true,"delivery":{"mode":"mock"}}'
```

Reset to blank local state:

```bash
curl -X POST http://127.0.0.1:8000/api/simulate/stop
curl -X POST http://127.0.0.1:8000/api/simulate/reset
```

## Shared Demo Profile

Use this profile when more than one person or partner may access the service. Put it behind HTTPS with a reverse proxy or trusted tunnel; do not expose raw HTTP on the public internet.

```bash
export REGENGINE_BASIC_AUTH_USERNAME=demo
export REGENGINE_BASIC_AUTH_PASSWORD='replace-with-a-strong-password'
export REGENGINE_DEFAULT_TENANT=demo-default
export REGENGINE_CORS_ORIGINS=https://demo.example.com
export REGENGINE_DATA_DIR=/data
export REGENGINE_REQUIRE_AUTH=1
export REGENGINE_MAX_TENANTS=100
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`REGENGINE_REQUIRE_AUTH=1` makes the process refuse to start if either Basic
Auth variable is missing, so a shared demo cannot silently come up open after a
variable is renamed or dropped.

Tenant-scoped smoke check:

```bash
curl -u "$REGENGINE_BASIC_AUTH_USERNAME:$REGENGINE_BASIC_AUTH_PASSWORD" \
  -H 'X-RegEngine-Tenant: partner-acme' \
  http://127.0.0.1:8000/api/health
```

Expected health context:

- `tenant`: `partner-acme`
- `auth.enabled`: `true`
- `auth.username`: configured username
- `auth.uses_default_storage`: `false`
- `status.config.persist_path`: `/data/tenants/partner-acme/events.jsonl` (`{REGENGINE_DATA_DIR}/tenants/{tenant_id}/events.jsonl` — the `/data` prefix comes from `REGENGINE_DATA_DIR` set above; with `REGENGINE_DATA_DIR` unset it is the relative `data/tenants/partner-acme/events.jsonl`)

Tenant selection notes:

- API clients can send `X-RegEngine-Tenant` directly.
- The number of distinct tenants one process will materialize is capped by
  `REGENGINE_MAX_TENANTS` (default `100`), counting cached controllers plus
  tenant directories already on disk and excluding the built-in default tenant.
  A request for a *new* tenant past the cap gets `429` with a message naming the
  limit; existing tenants keep working. The cap applies whether or not Basic Auth
  is enabled, because the tenant header is honored either way. Reclaim capacity
  with `DELETE /api/operator/tenants/{tenant_id}`, or raise the value.
- Browser dashboard requests use the authenticated username as the tenant unless a trusted proxy injects `X-RegEngine-Tenant`.
- If several partners need isolated dashboard sessions at the same time, use separate reverse-proxy routes that inject different tenant headers, or run separate service instances with different `REGENGINE_BASIC_AUTH_USERNAME` values.

Prepare a tenant-specific fixture:

```bash
curl -u "$REGENGINE_BASIC_AUTH_USERNAME:$REGENGINE_BASIC_AUTH_PASSWORD" \
  -H 'X-RegEngine-Tenant: partner-acme' \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/api/demo-fixtures/fresh_cut_transformation/load \
  -d '{"reset":true,"delivery":{"mode":"mock"}}'
```

Shared-demo operating notes:

- Keep delivery mode set to `mock` unless there is an explicit live-ingest trial.
- Use a distinct tenant value per partner or workshop.
- Rotate `REGENGINE_BASIC_AUTH_PASSWORD` between external demos.
- Keep `REGENGINE_CORS_ORIGINS` limited to the HTTPS origins that should run the browser dashboard; Basic Auth deployments reject state-changing browser requests from origins outside that list.
- Mount persistent storage at `REGENGINE_DATA_DIR` so event logs and scenario saves survive restarts.
- Back up or delete `data/tenants/{tenant_id}/` according to the partner's data-retention expectation.
- Use the protected `/api/operator/tenants` endpoints to list, reset, or delete tenant scopes instead of shelling into the volume during a live demo.

## Live Ingest Trial Profile

Use this only when a RegEngine workspace, API key, tenant id, and endpoint target are approved for the trial. The application still starts in mock mode; live delivery is enabled in the request or dashboard controls.

Start the server with shared-demo protections:

```bash
export REGENGINE_BASIC_AUTH_USERNAME=demo
export REGENGINE_BASIC_AUTH_PASSWORD='replace-with-a-strong-password'
export REGENGINE_DEFAULT_TENANT=live-trial
export REGENGINE_CORS_ORIGINS=https://live-trial.example.com
export REGENGINE_DATA_DIR=/data
export REGENGINE_REQUIRE_AUTH=1
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Live delivery targets are checked before any credential header is built: only
`http(s)` endpoints are allowed, and loopback, private, link-local and
cloud-metadata hosts are refused (including a public hostname that resolves to
one). For an approved trial against a deployed RegEngine no extra configuration
is needed. Optionally set `REGENGINE_ALLOWED_DELIVERY_HOSTS` (comma-separated;
a leading dot matches subdomains, e.g. `.regengine.co`) to pin delivery to the
approved workspace host and refuse everything else. Only a deliberately local
RegEngine stack needs `REGENGINE_ALLOW_PRIVATE_DELIVERY_HOSTS=1`; never set it
on a shared or deployed profile.

Preferred gated script flow:

```bash
export REGENGINE_REMOTE_BASE_URL=https://regengine-inflow-lab-gh-production.up.railway.app
export REGENGINE_REMOTE_USERNAME=demo
export REGENGINE_REMOTE_PASSWORD='<shared-demo-password>'
export REGENGINE_REMOTE_TENANT=live-trial
export REGENGINE_LIVE_ENDPOINT=https://www.regengine.co/api/v1/webhooks/ingest
export REGENGINE_LIVE_API_KEY='<approved-live-key>'
export REGENGINE_LIVE_TENANT_ID='<approved-live-tenant-id>'

# Mock dry-run only. This sends no live RegEngine traffic.
uv run python scripts/live_trial.py --dry-run-only

# Live trial. This first performs the mock dry-run, then sends exactly one live batch.
uv run python scripts/live_trial.py --confirm-live
```

The script refuses to run without either `--dry-run-only` or `--confirm-live`. It never prints the Basic Auth password, live API key, or live tenant id. Stop after the first live result and review the posted/failed status before any further volume.

Dry-run the exact scenario without live traffic:

```bash
curl -u "$REGENGINE_BASIC_AUTH_USERNAME:$REGENGINE_BASIC_AUTH_PASSWORD" \
  -H 'X-RegEngine-Tenant: live-trial' \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/api/simulate/reset \
  -d '{"scenario":"fresh_cut_processor","batch_size":1,"seed":204,"delivery":{"mode":"mock"}}'

curl -u "$REGENGINE_BASIC_AUTH_USERNAME:$REGENGINE_BASIC_AUTH_PASSWORD" \
  -H 'X-RegEngine-Tenant: live-trial' \
  -X POST http://127.0.0.1:8000/api/simulate/step
```

Set the live delivery config only after the dry run looks correct:

```bash
export REGENGINE_LIVE_API_KEY='replace-with-live-key'
export REGENGINE_LIVE_TENANT_ID='replace-with-live-tenant-id'

curl -u "$REGENGINE_BASIC_AUTH_USERNAME:$REGENGINE_BASIC_AUTH_PASSWORD" \
  -H 'X-RegEngine-Tenant: live-trial' \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/api/simulate/reset \
  --data-binary @- <<JSON
{
  "source": "codex-simulator",
  "scenario": "fresh_cut_processor",
  "batch_size": 1,
  "seed": 204,
  "delivery": {
    "mode": "live",
    "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
    "api_key": "${REGENGINE_LIVE_API_KEY}",
    "tenant_id": "${REGENGINE_LIVE_TENANT_ID}"
  }
}
JSON
```

Then send exactly one live event batch:

```bash
curl -u "$REGENGINE_BASIC_AUTH_USERNAME:$REGENGINE_BASIC_AUTH_PASSWORD" \
  -H 'X-RegEngine-Tenant: live-trial' \
  -X POST 'http://127.0.0.1:8000/api/simulate/step?batch_size=1'
```

If using the dashboard instead of curl, set `Delivery` to `Connected (live RegEngine)` in the *RegEngine connection* panel, enter the API key and tenant id, leave the endpoint blank to use the documented default, click `Save settings`, and click `Record next batch` first. Direct browser sessions use the Basic Auth username as the storage tenant unless a proxy injects `X-RegEngine-Tenant`. Avoid starting the loop until one live batch is accepted.

Live-trial safeguards:

- Keep `batch_size` at `1` for the first live request.
- Confirm the dashboard delivery monitor shows `posted` before increasing volume.
- If delivery fails, do not keep retrying with the same credentials blindly; inspect the displayed error and confirm endpoint, API key, and tenant id.
- Use `POST /api/delivery/retry` only after correcting the delivery config.
- Keep the live-trial dashboard origin explicit in `REGENGINE_CORS_ORIGINS`; do not use wildcard CORS with Basic Auth.
- Do not commit API keys, tenant ids, partner names, downloaded exports, or event logs from live trials.

## Service Wrappers

For a persistent local or shared demo service, use the macOS LaunchAgent, Linux systemd unit, or Docker examples in `README.md`. Keep these profile choices the same inside the service wrapper:

- Local demo: bind `127.0.0.1`, Basic Auth unset.
- Shared demo: bind to the private interface or proxy target, Basic Auth set, CORS origins explicit.
- Live trial: prefer private network access, Basic Auth set, CORS origins explicit, and live delivery enabled only per operator action.

## Railway Shared Demo

The repo includes `Dockerfile` and `railway.json` for Railway. Recommended Railway variables:

```bash
REGENGINE_BASIC_AUTH_USERNAME=demo
REGENGINE_BASIC_AUTH_PASSWORD=<strong generated password>
REGENGINE_DEFAULT_TENANT=demo-default
REGENGINE_CORS_ORIGINS=https://<railway-domain>
REGENGINE_DATA_DIR=/data
REGENGINE_BUILD_SHA=<deployed git sha>
REGENGINE_BUILD_BRANCH=main
```

The image already sets `REGENGINE_REQUIRE_AUTH=1`, so a Railway deploy missing
`REGENGINE_BASIC_AUTH_USERNAME` or `REGENGINE_BASIC_AUTH_PASSWORD` fails to
start rather than serving the demo without credentials. Do not set
`REGENGINE_REQUIRE_AUTH=0` on a shared service. `REGENGINE_MAX_TENANTS`
(default `100`) bounds how many tenant scopes the service will create.

Attach a Railway volume at `/data` before using the service for partner demos. After a Railway domain is generated, update `REGENGINE_CORS_ORIGINS` to that exact HTTPS origin.

### Automated deploys (preferred)

The shared demo service (`regengine-inflow-lab-gh` on Railway) is connected
to this GitHub repository, so **every push to `main` deploys automatically**
— no repository secrets, no workflow, no manual ritual. `/api/healthz`
reports the deployed commit from Railway's own `RAILWAY_GIT_COMMIT_SHA`, so
drift between `main` and the live service is directly observable (and the
nightly Remote Smoke checks assert on it).

A CLI-driven deploy workflow (`deploy.yml` + `RAILWAY_TOKEN`) existed
briefly before the GitHub connection; it was removed because two publishers
racing on one service make deploy history impossible to read.

> Deploying by hand is what let the shared demo drift 47 commits behind `main`
> for roughly 100 days: the ritual below is easy to forget and nothing failed
> loudly when it was. Prefer the workflow.

### Moving the demo to a new service (cutover checklist)

Standing up a replacement service and retiring the old one has ordering
traps. Work through these in sequence; every step is verifiable before the
next one starts.

1. **Set `REGENGINE_CORS_ORIGINS` to the NEW service's own origin — change
   it, never copy or reference it.** This value names the host, so a Railway
   reference variable (`${{old-service.REGENGINE_CORS_ORIGINS}}`) carries the
   *old* URL and fails in two ways at once: browser requests lose their CORS
   headers, and every state-changing request is rejected as an untrusted
   origin (`app/auth_middleware.py` gates writes on the same list).
   Verify: `curl -sD - -o /dev/null -H "Origin: https://<new-domain>" https://<new-domain>/api/healthz`
   must echo the origin back in `access-control-allow-origin`.
   (On Railway the service now also trusts its own `RAILWAY_PUBLIC_DOMAIN`
   origin *in addition to* this list, so a stale configured value can no
   longer lock the service out of its own domain — but explicit origins for
   any other dashboard host still need this step.)
2. **Replace secret reference variables with concrete values.**
   `REGENGINE_BASIC_AUTH_USERNAME`, `REGENGINE_BASIC_AUTH_PASSWORD`, and
   `REGENGINE_WEBHOOK_HMAC_SECRET` may reference the old service. Deleting
   the old service while references remain breaks auth on the new one.
3. **Retarget every consumer of the old URL.** In this repo that is the
   smoke workflows (`remote-smoke.yml`, `remote-browser-smoke.yml`,
   `smoke-failure-issue.yml`) and the docs. Outside this repo, the RegEngine
   dashboard reaches the demo through a Next.js proxy route **hosted on
   Vercel**, so `INFLOW_LAB_SERVICE_URL` (fallback
   `NEXT_PUBLIC_INFLOW_LAB_SERVICE_URL`) is a *Vercel* env var — it is not
   on any Railway service, and Vercel bakes env at build, so the change is
   inert until production is redeployed.
   Verify: `https://<dashboard-host>/api/inflow-lab/api/healthz` reports the
   new commit with `commit_source: RAILWAY_GIT_COMMIT_SHA`.
4. **Carry the persistent volume across before sending traffic.** The demo
   writes its event history to `REGENGINE_DATA_DIR` (`app/tenancy.py:22`,
   `/data/tenants/{tenant_id}/events.jsonl` in production), and on Railway that
   path only survives a redeploy if a volume is mounted there. A service
   created fresh has none, and nothing about the running service says so: it
   answers 200, serves the right contract, reports the right build identity,
   and quietly starts from an empty store after every deploy — which for a
   GitHub-connected service is *every push to `main`*.
   Verify: the new service's config must carry a `volumeMounts` entry whose
   `mountPath` matches `REGENGINE_DATA_DIR` on the old one. In the August 2026
   cutover the old service mounted a volume at `/data` and the replacement had
   no `volumeMounts` key at all, which would have discarded the demo's history
   on the first push after the switch.
5. **Only then retire the old service.** Until step 3 lands everywhere, the
   old service is the live backend for whatever still points at it. Note that
   the volume belongs to the old service — deleting it destroys the data
   unless it has been migrated or detached first.

> Steps 1–2 are exactly what the GitHub-connected cutover missed in August
> 2026: the new service referenced the old one's variables, the URL-bearing
> CORS value came across stale, and both nightly smokes stayed red for three
> days after the cutover PR merged — with the failure attributed to the wrong
> cause until the allowlist was probed directly.

`scripts/cutover_preflight.sh <old-url> <new-url>` mechanises what steps 1 and
3 can be checked from outside: every read-only path in the dashboard's proxy
contract answering alike on both services, the new service reporting
GitHub-injected build identity rather than a stale `REGENGINE_BUILD_SHA`, and
the new service trusting its own origin. It is read-only — the demo is shared,
and the POST routes the dashboard proxies mutate its state.

It deliberately cannot check steps 2 or 4, and says so on success rather than
implying a clean bill of health. Both are invisible from outside: a service
with the wrong credentials and a service with the right ones both answer 401,
and a service with no volume is indistinguishable from one with a volume until
the next redeploy discards the data. The Basic-auth credentials live as secrets on
two different platforms, so from outside a service with the *wrong* credentials
is indistinguishable from one with the right ones — both answer 401 to an
unauthenticated probe. Vercel's `INFLOW_LAB_BASIC_AUTH_USERNAME` / `_PASSWORD`
must equal the new service's `REGENGINE_BASIC_AUTH_USERNAME` / `_PASSWORD`, as
concrete values. If they differ, every proxied call answers 401 the moment
`INFLOW_LAB_SERVICE_URL` is flipped.

Verify the flip on `/api/simulate/status`, not `/api/healthz`: the proxy
answers HTTP 200 with `{"offline":true}` when the backend is unreachable
(`optionalOfflineResponse`), so a 200 on the health path alone proves nothing.

### Manual CLI deploy (fallback)

When deploying from the CLI, update the non-secret build variables before `railway up` so health checks can identify stale deployments:

```bash
railway variable set --skip-deploys REGENGINE_BUILD_SHA="$(git rev-parse HEAD)" \
  REGENGINE_BUILD_BRANCH="$(git branch --show-current)"
railway up --ci -m "Deploy $(git rev-parse --short HEAD)"
```

`REGENGINE_BUILD_SHA` takes precedence over Railway's own
`RAILWAY_GIT_COMMIT_SHA` (see `app/build_info.py`), and `.dockerignore`
excludes `.git`, so this variable is the container's only source of build
identity. A stale value makes `/api/healthz` report a commit that is not
deployed. If you later switch the service to Railway's GitHub integration,
**delete `REGENGINE_BUILD_SHA` and `REGENGINE_BUILD_BRANCH`** so Railway's
injected metadata is used instead.

Validate the deployed Railway demo with the remote smoke harness:

```bash
export REGENGINE_REMOTE_BASE_URL=https://regengine-inflow-lab-gh-production.up.railway.app
export REGENGINE_REMOTE_USERNAME=demo
export REGENGINE_REMOTE_PASSWORD='<shared-demo-password>'
export REGENGINE_REMOTE_TENANT=remote-smoke
uv run --no-dev python scripts/remote_smoke.py
```

The harness keeps delivery in `mock` mode, uses the dedicated smoke tenant by default, and verifies health, Basic Auth, CORS, fixture load, lineage, FDA CSV, and EPCIS JSON-LD without printing the password.

Validate the deployed browser dashboard through the same shared-demo auth path:

```bash
export REGENGINE_BROWSER_BASE_URL=https://regengine-inflow-lab-gh-production.up.railway.app
export REGENGINE_BROWSER_USERNAME=demo
export REGENGINE_BROWSER_PASSWORD='<shared-demo-password>'
export REGENGINE_BROWSER_TENANT=remote-browser-smoke
uv run --no-dev --group browser python scripts/browser_smoke.py
```

The browser smoke forces dashboard delivery back to `mock`, uses the dedicated browser-smoke tenant, and verifies the real dashboard start/stop, reset, single-batch, fixture, lineage, and CSV warning flows without printing the password.

You can run the same checks from GitHub Actions with the manual and nightly **Remote Smoke** and **Remote Browser Smoke** workflows. Configure these repository secrets first:

```text
REGENGINE_REMOTE_USERNAME=demo
REGENGINE_REMOTE_PASSWORD=<shared-demo-password>
```

Then run `.github/workflows/remote-smoke.yml` or `.github/workflows/remote-browser-smoke.yml` from the Actions tab. The workflow inputs are:

| Input | Default | Purpose |
|---|---|---|
| `base_url` | `https://regengine-inflow-lab-gh-production.up.railway.app` | Deployed shared-demo URL to validate |
| `tenant` | `remote-smoke` or `remote-browser-smoke` | Tenant used for isolated smoke data |

The workflows install repo dependencies with `uv` and run `scripts/remote_smoke.py` or `scripts/browser_smoke.py` through `uv run`. They do not require live RegEngine credentials and keep delivery in `mock` mode. Nightly scheduled runs target the Railway shared-demo URL with `remote-smoke-nightly` and `remote-browser-smoke-nightly` tenants, and both remote workflows compare `/api/healthz` build metadata to the workflow commit: `remote-smoke.yml` passes `REGENGINE_EXPECTED_BUILD_SHA` and `remote-browser-smoke.yml` passes `REGENGINE_BROWSER_EXPECTED_BUILD_SHA` (which falls back to `REGENGINE_EXPECTED_BUILD_SHA`). A `build commit mismatch` failure from either means the deployed service is not running the workflow's commit — redeploy or wait for the deploy to finish, rather than hunting a smoke-flow bug.

Railway log triage:

```bash
railway logs --lines 100
railway logs --http --status ">=400" --lines 50
railway logs --http --path /api/health --lines 20
```

Application request logs include `method`, `path`, `status`, `duration_ms`, `tenant`, and `delivery_mode`. They do not include Authorization headers, API keys, query strings, request bodies, response bodies, FDA CSV contents, or EPCIS export contents.

Use these patterns when diagnosing a shared demo:

- `status=401` on API routes usually means the Basic Auth username/password in the operator environment or GitHub secret is wrong.
- Missing browser CORS headers usually means `REGENGINE_CORS_ORIGINS` does not exactly match the deployed HTTPS origin.
- `status=403` on simulator actions with valid Basic Auth usually means the browser `Origin` or `Referer` is not in `REGENGINE_CORS_ORIGINS`.
- Empty state after restart usually means the Railway volume is missing or `REGENGINE_DATA_DIR` is not `/data`.
- A failing platform healthcheck with the process up usually means `/api/healthz` returned `503` because the event store is unwritable — check the volume mount, disk usage, and permissions on `REGENGINE_DATA_DIR`.
- A container that exits at startup with `REGENGINE_REQUIRE_AUTH is set but ...` is missing `REGENGINE_BASIC_AUTH_USERNAME`/`REGENGINE_BASIC_AUTH_PASSWORD`.
- `status=429` on a first request for a new tenant means `REGENGINE_MAX_TENANTS` is reached; delete unused tenant scopes or raise the cap.
- Live delivery failures should be diagnosed from the dashboard delivery monitor and sanitized record status before retrying with corrected live endpoint, API key, and tenant id.
- Uvicorn startup `INFO` lines can appear as `level=error` in Railway logs. Treat that as log-label noise unless there is a Python traceback, failed deployment status, or HTTP 5xx.

## Profile Verification Checklist

- `GET /api/health` returns the expected tenant, auth context, and build metadata.
- `GET /api/healthz` returns `{"ok": true, "build": ...}` with HTTP 200 and without credentials for platform healthchecks. It answers `503` with `"ok": false` and a `store` block when the default tenant's event store cannot be written, so a failing platform healthcheck on a running process points at the volume at `REGENGINE_DATA_DIR` first. `GET /api/health` reports the same `store` block but stays 200 so the console still renders.
- `build.commit_sha_short` matches the deployed git commit before manual or nightly remote smoke runs.
- Browser requests from the intended HTTPS origin receive the `access-control-allow-origin` response header; untrusted origins do not.
- `REGENGINE_DATA_DIR` points at mounted persistent storage in shared-demo and live-trial deployments.
- Dashboard stats match the chosen tenant/auth/storage profile.
- `POST /api/demo-fixtures/fresh_cut_transformation/load` succeeds in `mock` mode.
- `uv run --no-dev python scripts/remote_smoke.py` passes for the deployed shared-demo URL.
- The manual GitHub **Remote Smoke** workflow passes with the same shared-demo URL and smoke tenant.
- The manual GitHub **Remote Browser Smoke** workflow passes with the same shared-demo URL and browser-smoke tenant.
- Nightly GitHub **Remote Smoke** and **Remote Browser Smoke** schedules are enabled after the shared-demo secrets are configured.
- Lineage for `TLC-DEMO-FC-OUT-001` includes upstream harvest and packed lots.
- FDA CSV and EPCIS exports are derivable from stored records.
- No generated `data/` files or secrets are staged before committing.

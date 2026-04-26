# Deployment Profiles

This guide gives concrete run profiles for local development, shared design-partner demos, and live-ingest trials. All profiles preserve mock mode as the default unless live delivery is explicitly configured per request.

## Profile Matrix

| Profile | Bind address | Auth | Storage | Delivery default | Best for |
|---|---|---|---|---|---|
| Local demo | `127.0.0.1` | Off | `data/events.jsonl` | `mock` | Solo development and screen-share demos |
| Shared demo | `0.0.0.0` behind TLS/proxy | Basic Auth on | `data/tenants/{tenant_id}/` | `mock` | Design partners, multiple tenants, non-live workshops |
| Live ingest trial | Prefer private host or VPN | Basic Auth on | Tenant-scoped | `mock`; switch request to `live` | Controlled RegEngine workspace validation |

## Common Prerequisites

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
python3 scripts/smoke_regression.py
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
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

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
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

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
- `status.config.persist_path`: `data/tenants/partner-acme/events.jsonl`

Tenant selection notes:

- API clients can send `X-RegEngine-Tenant` directly.
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
- Keep `REGENGINE_CORS_ORIGINS` limited to the HTTPS origins that should run the browser dashboard.
- Mount persistent storage at `REGENGINE_DATA_DIR` so event logs and scenario saves survive restarts.
- Back up or delete `data/tenants/{tenant_id}/` according to the partner's data-retention expectation.

## Live Ingest Trial Profile

Use this only when a RegEngine workspace, API key, tenant id, and endpoint target are approved for the trial. The application still starts in mock mode; live delivery is enabled in the request or dashboard controls.

Start the server with shared-demo protections:

```bash
export REGENGINE_BASIC_AUTH_USERNAME=demo
export REGENGINE_BASIC_AUTH_PASSWORD='replace-with-a-strong-password'
export REGENGINE_DEFAULT_TENANT=live-trial
export REGENGINE_CORS_ORIGINS=https://live-trial.example.com
export REGENGINE_DATA_DIR=/data
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

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

If using the dashboard instead of curl, set delivery mode to `Live RegEngine`, enter the API key and tenant id, leave the endpoint blank to use the documented default, and click `Single batch` first. Direct browser sessions use the Basic Auth username as the storage tenant unless a proxy injects `X-RegEngine-Tenant`. Avoid starting the loop until one live batch is accepted.

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
```

Attach a Railway volume at `/data` before using the service for partner demos. After a Railway domain is generated, update `REGENGINE_CORS_ORIGINS` to that exact HTTPS origin.

Validate the deployed Railway demo with the remote smoke harness:

```bash
export REGENGINE_REMOTE_BASE_URL=https://regengine-inflow-lab-production.up.railway.app
export REGENGINE_REMOTE_USERNAME=demo
export REGENGINE_REMOTE_PASSWORD='<shared-demo-password>'
export REGENGINE_REMOTE_TENANT=remote-smoke
python3 scripts/remote_smoke.py
```

The harness keeps delivery in `mock` mode, uses the dedicated smoke tenant by default, and verifies health, Basic Auth, CORS, fixture load, lineage, FDA CSV, and EPCIS JSON-LD without printing the password.

## Profile Verification Checklist

- `GET /api/health` returns the expected tenant and auth context.
- `GET /api/healthz` returns `{"ok": true, ...}` without credentials for platform healthchecks.
- Browser requests from the intended HTTPS origin receive the `access-control-allow-origin` response header; untrusted origins do not.
- `REGENGINE_DATA_DIR` points at mounted persistent storage in shared-demo and live-trial deployments.
- Dashboard stats match the chosen tenant/auth/storage profile.
- `POST /api/demo-fixtures/fresh_cut_transformation/load` succeeds in `mock` mode.
- `python3 scripts/remote_smoke.py` passes for the deployed shared-demo URL.
- Lineage for `TLC-DEMO-FC-OUT-001` includes upstream harvest and packed lots.
- FDA CSV and EPCIS exports are derivable from stored records.
- No generated `data/` files or secrets are staged before committing.

# RegEngine Production Ingest Arena PRD

## Product Idea

Create a dedicated RegEngine live ingest target that behaves like a controlled production arena: Inflow Lab sends realistic FSMA 204 traffic, RegEngine accepts or rejects it through the real production pipeline, and operators can watch each batch move through the system like a visible track.

The point is not just load. The point is observable confidence.

## Why We Care

The current single-batch live trial answers whether one live request can work. A production ingest arena answers whether a real tenant, real API key, real HMAC signature, real subscription gate, real persistence, and real rate limits work together under a gradual ramp.

## Experience Goal

The operator should feel like they are watching a run unfold:

```text
Inflow Lab batch
  -> API key gate
  -> HMAC gate
  -> tenant resolution
  -> subscription gate
  -> webhook rate limit
  -> KDE validation
  -> canonical persistence
  -> hash/audit chain
  -> export-ready evidence
```

Each stage should expose enough state to answer: where did the batch go, did it pass, how long did it take, and what stopped it?

## Production Target Requirements

- Dedicated tenant UUID for pressure traffic.
- Tenant-bound API key with `webhooks.ingest` scope or `*` only for tightly controlled operator use.
- `REGENGINE_ENV=production` on the RegEngine backend.
- `DATABASE_URL` pointing at the production/staging Postgres instance.
- `REGENGINE_API_KEY` or `API_KEY` set so the webhook auth guard cannot silently accept traffic on a misconfigured deploy.
- `WEBHOOK_HMAC_SECRET` set on RegEngine.
- Matching `REGENGINE_WEBHOOK_HMAC_SECRET` set in the Inflow Lab process.
- `REDIS_URL` set and subscription status present for the tenant, or an explicit incident-only `SUBSCRIPTION_GATE_FAIL_OPEN=true` override.
- Conservative `WEBHOOK_INGEST_RATE_LIMIT_RPM` and `INGESTION_RBAC_RATE_LIMITS` values for the first run.
- Router surface kept narrow with non-core/experimental routers disabled.

## Inflow Lab Requirements

- `REGENGINE_REMOTE_BASE_URL` points at the Inflow Lab instance.
- `REGENGINE_REMOTE_USERNAME` and `REGENGINE_REMOTE_PASSWORD` authenticate to that Inflow Lab instance.
- `REGENGINE_REMOTE_TENANT` identifies the operator-side pressure run.
- `REGENGINE_LIVE_ENDPOINT` points at RegEngine `POST /api/v1/webhooks/ingest`.
- `REGENGINE_LIVE_API_KEY` is the tenant-bound RegEngine API key.
- `REGENGINE_LIVE_TENANT_ID` is the target RegEngine tenant UUID.

## Provisioning Command

Provision the RegEngine-side arena from the RegEngine repo:

```bash
cd /Users/sellers/RegEngine/repo

python3 scripts/provision_ingest_arena.py \
  --require-hmac \
  --live-endpoint https://www.regengine.co/api/v1/webhooks/ingest \
  --write-env /tmp/regengine-inflow-arena.env
```

Then load `/tmp/regengine-inflow-arena.env` in the Inflow Lab shell before running `scripts/pressure_trial.py`.

## First Arena Run

Use one worker and low volume:

```bash
python3 scripts/pressure_trial.py \
  --confirm-live \
  --stages 2x1,3x5,3x10 \
  --workers 1 \
  --max-failures 0 \
  --max-error-rate 0 \
  --watch \
  --report-json output/pressure/arena-smoke.json
```

Then add parallel tenant-scoped worker streams:

```bash
python3 scripts/pressure_trial.py \
  --confirm-live \
  --stages 3x10,5x25,5x50 \
  --workers 2 \
  --max-failures 0 \
  --max-error-rate 0 \
  --max-p95-ms 5000 \
  --timeout-seconds 120 \
  --watch \
  --report-json output/pressure/arena-workers.json
```

For larger batches, set `REGENGINE_LIVE_TIMEOUT_SECONDS=60` or higher on the Inflow Lab process so a slow-but-successful RegEngine ingest does not get recorded as a delivery failure. Use `--timeout-seconds` on `scripts/pressure_trial.py` for the outer harness timeout.

Render the JSON report as a terminal gravitram:

```bash
python3 scripts/pressure_gravitram.py output/pressure/arena-workers.json
```

The track symbols are `.` for fast batches, `o` for normal batches, `S` for slow batches, `H` for hot batches, and `!` for failed batches. This is intentionally compact so an operator can see whether one worker or request jammed while the aggregate stage still looks healthy.

## Gravitram Watch Mode

Near-term watch mode is terminal-first. Each completed live batch emits one line:

```text
batch stage=1 request=1/3 tenant=pressure-trial-w1 batch_size=10 generated=10 posted=10 failed=0 latency_ms=214.8
```

The later browser version should turn those lines, JSON reports, or a server-sent-event stream into a track view where batches visibly move from source to persistence/export readiness.

## Success Criteria

- The arena can accept a one-worker smoke ramp with zero failed events.
- The arena can produce a JSON report for later comparison.
- Operators can identify the first failing gate when a run fails.
- A run never uses customer production tenant data unless explicitly approved.
- A run can be repeated after ingest changes with the same stage plan.

## Stop Conditions

- Any failed live batch when `--max-failures 0`.
- Any nonzero error rate when `--max-error-rate 0`.
- p95 latency exceeding the run gate.
- RegEngine returns 401, 402, 403, 429, or 503.
- The report shows accepted counts that do not match generated counts.

## Open Follow-Ups

- Add a browser visualization that consumes JSONL or SSE progress events.
- Add RegEngine-side per-gate timing so failures can be attributed without log spelunking.
- Add cleanup/rotation tooling for arena tenants and one-time API keys after pressure campaigns.

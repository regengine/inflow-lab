# RegEngine Inflow Lab

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Inflow Lab plays the role of **a RegEngine customer's own software**: a fictional factory's production system ("Meridian Fresh Foods — Plant Operations Console") that generates FSMA 204 CTE events through a realistic supply-chain lifecycle and delivers them to RegEngine exactly the way a real integrator does — API key, tenant header, idempotency keys, optional HMAC signing, and real per-event accept/reject responses. Ships with a FastAPI backend, the operator console, a built-in RegEngine stand-in that mirrors the live webhook's validation, and an end-to-end customer-journey harness.

> **Non-production sandbox.** Inflow Lab is a demo, onboarding, and integration-validation tool for RegEngine — not a product, not a source of compliance record, and not built for public exposure. Meridian Fresh Foods is fictional and all data is synthetic. Without Basic Auth it serves a single shared `local-demo` tenant; keep it on localhost or behind auth, and never enter real data. See [REPO_PURPOSE.md](REPO_PURPOSE.md).

## Table of contents

- [What it does](#what-it-does)
- [Project layout](#project-layout)
- [Quick start (local dev)](#quick-start-local-dev)
- [Running tests](#running-tests)
- [Browser smoke](#browser-smoke)
- [Release smoke regression](#release-smoke-regression)
- [Delivery modes](#delivery-modes)
- [RegEngine integration settings](#regengine-integration-settings)
- [Customer journey harness](#customer-journey-harness)
- [Basic auth and tenant storage](#basic-auth-and-tenant-storage)
- [Replay mode](#replay-mode)
- [CSV import](#csv-import)
- [Scenario presets](#scenario-presets)
- [Demo fixtures](#demo-fixtures)
- [FDA export presets](#fda-export-presets)
- [EPCIS 2.0 export scaffolding](#epcis-20-export-scaffolding)
- [Full FSMA simulation](#full-fsma-simulation)
- [Design-partner demo script](#design-partner-demo-script)
- [Deployment profiles](#deployment-profiles)
- [API reference](#api-reference)
- [RegEngine payload contract](#regengine-payload-contract)
- [Deployment](#deployment)
  - [macOS LaunchAgent (auto-start on login)](#macos-launchagent-auto-start-on-login)
  - [Linux systemd unit](#linux-systemd-unit)
  - [Docker (optional)](#docker-optional)
- [Logs and troubleshooting](#logs-and-troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## What it does

Inflow Lab reproduces the full experience a RegEngine customer has in the wild, playing the customer's side of the integration:

- **A factory persona.** The console presents as Meridian Fresh Foods' own production software — line setup, line control, a shift log, traceability views — with RegEngine demoted to an *Integrations* settings panel the way a real customer configures a third-party service (endpoint, API key, tenant, test connection). First-time visitors get a welcome dialog and an optional five-step guided tour of the workspace (persisted per browser, re-launchable via *Take the tour*).
- **A realistic event generator.** Lots walk through a believable supply-chain lifecycle so the resulting trace feels legitimate rather than random:
  1. **Harvesting** originates at farms
  2. **Cooling** moves harvested lots through cooler facilities
  3. **Initial packing** creates downstream packed lots
  4. **Shipping** creates a believable destination and reference document
  5. **Receiving** corresponds to an actual prior shipment
  6. **Transformation** consumes input lots and emits a new output lot
  7. **Downstream shipping + receiving** moves transformed lots to DCs and retail
- **Real integration mechanics.** Live delivery sends exactly what an integrator sends — `X-RegEngine-API-Key`, `X-Tenant-ID`, a required `Idempotency-Key`, optional HMAC body signing — and surfaces RegEngine's real per-event accept/reject responses. The built-in stand-in mirrors the live webhook's validation, so a payload that would fail in production fails here too.
- **Real friction, on purpose.** Missing-KDE rejections, bad-key 401s, lapsed-billing 402s, and rate-limit 429s can all be rehearsed, with recovery guidance and idempotent retries that never double-ingest.

Each event accepted by **mock or live delivery** is persisted with `event_id`, `sha256_hash`, and `chain_hash` so the flow feels production-like, and you can trace transitive lot lineage forward and backward through the console or API. Those three come from RegEngine's ingest response, so records generated in `none` mode — which persists locally without delivering — carry lineage but no hash chain. Seed fixtures in `mock` mode if you want the evidence chain populated.

## Project layout

```text
app/
  audit.py               # Scenario audit scoring for the console's readiness lens
  auth.py                # Optional Basic Auth and tenant context resolution
  auth_middleware.py     # Auth + tenant middleware, trusted-origin checks, request logging
  build_info.py          # Public non-secret build/deployment metadata for health checks
  controller.py          # Simulator lifecycle (start/stop/step/reset) and delivery fan-out
  csv_importer.py        # CSV parsing for scheduled events and seed lots
  cte_rules.py           # Required/recommended KDEs per CTE (pinned to RegEngine's contract)
  demo_fixtures.py       # Deterministic demo playback fixtures (RegEngine-canonical KDEs)
  engine.py              # CTE generation and lot lineage logic
  epcis_export.py        # EPCIS 2.0 JSON-LD export scaffolding
  fda_export.py          # FDA-request CSV export presets and rendering
  industry_adapters.py   # Industry-specific event shaping (produce, seafood, dairy)
  main.py                # FastAPI app and route wiring
  mock_service.py        # RegEngine stand-in mirroring the live webhook's validation
  regengine_client.py    # HTTP client for live delivery + connection-check probe
  routers/               # API routers (simulation, integration, events, exports, ...)
  scenario_saves.py      # Per-scenario saved config and event-log snapshots
  scenarios.py           # Named scenario presets for product/location/flow mixes
  schemas/               # Pydantic models (domain, simulation, ingestion, integration, ...)
  store.py               # Event persistence (JSONL)
  static/                # Operator console (vanilla JS, HTML, CSS)
.agents/skills/regengine-api-contract/
.github/
  codex/prompts/autobuild.md
  workflows/ci.yml
  workflows/codex-autopilot.yml
  workflows/remote-smoke.yml
scripts/
  _smoke_common.py       # Shared assertion/redaction harness for the smoke scripts
  smoke_regression.py    # End-to-end API smoke for demo-ready release checks
  browser_smoke.py       # Headless Playwright dashboard smoke
  remote_smoke.py        # HTTP smoke harness for deployed shared-demo instances
  live_trial.py          # Gated one-batch live-ingest trial runner
  customer_journey.py    # End-to-end customer journey against a real RegEngine stack
tests/
.dockerignore
AGENTS.md                # Repository instructions for Codex-style agents
AUTOPILOT_TASKS.md       # Standing backlog for unattended runs
DEPLOYMENT_PROFILES.md   # Local, shared-demo, and live-ingest run profiles
DESIGN_PARTNER_DEMO_SCRIPT.md  # Design-partner walkthrough and reset script
Dockerfile               # Container image for shared demo deployments
docker-entrypoint.sh     # Chowns mounted data dir, then drops to app user
PROMPT_FOR_CODEX.md      # Paste-ready Codex task prompt
RELEASE_CHECKLIST.md     # Demo-ready release gate
pyproject.toml
uv.lock                  # Locked dependency graph for uv installs
railway.json             # Railway Docker build and healthcheck config
```

## Quick start (local dev)

Requires **Python 3.11+**.

```bash
# From the project root
python3 -m pip install --upgrade uv
uv sync --group dev

# Run the dev server (auto-reload)
uv run uvicorn app.main:app --reload
```

Then open:

```
http://127.0.0.1:8000
```

The console presents as Meridian Fresh Foods' plant-operations software: line setup (operation type, line profile, data sets), line control (start/pause/step/clear with live metrics), a **RegEngine connection** integrations panel (endpoint, API key, tenant, test-connection verdicts, failure-mode rehearsal toggles), a shift log with per-event delivery status, lot tracing, an audit-readiness lens, CSV import, and FDA CSV / EPCIS 2.0 JSON-LD evidence exports. The delivery monitor explains how to recover from each failure class (bad key, lapsed billing, rate limit, missing KDEs). It subscribes to live status/event snapshots with Server-Sent Events and falls back to refresh polling if the stream disconnects. Delivery defaults to **`mock`** (the built-in RegEngine stand-in) so no credentials are required.

Event records are stored as JSONL at `config.persist_path` (`data/events.jsonl` by default for local unauthenticated use). Set `REGENGINE_DATA_DIR` to move the default event log, tenant logs, and scenario saves under another directory such as `/data` for a mounted deployment volume. Existing records at that path are loaded when the app starts or when a start/reset request points at a different path; reset clears the currently configured event log. Tenant-scoped requests store records under `{REGENGINE_DATA_DIR}/tenants/{tenant_id}/events.jsonl` and ignore untrusted persist-path overrides. Lineage, stats, retry lookup, replay, FDA CSV, and EPCIS exports read the persisted JSONL history so older records remain available after the in-memory recent-events window rolls forward. Replay reads the JSONL log without appending, duplicating, or rewriting stored events.

## Running tests

```bash
uv run pytest
```

The suite covers payload shape, engine determinism, the HTTP API contract, the integration-settings endpoints, mock rejection/friction/idempotency behavior, and a contract-pin test that locks `app/cte_rules.py`'s required KDEs to RegEngine's live webhook table (`webhook_models.REQUIRED_KDES_BY_CTE`) so validator drift fails CI instead of failing a customer.

## Browser smoke

Run the dashboard smoke after frontend or operator-flow changes:

```bash
uv sync --no-dev --group browser
uv run --no-dev --group browser playwright install chromium
uv run --no-dev --group browser python scripts/browser_smoke.py
```

The smoke starts a temporary local server with mock delivery, drives Chromium through the first-run welcome and guided tour (including onboarding persistence across a reload), the console start/pause, two-step clear confirm, single-batch, connection-test, line-data load, transformed-lot lineage lookup, Enter-to-trace, and CSV warning display flows, then exits nonzero with a clear failure message if a browser assertion fails. It forces the delivery mode to `mock` before taking any action. Set `REGENGINE_BROWSER_EXECUTABLE` to point at a pre-installed Chromium instead of downloading one (useful in sandboxed CI environments).

Set `REGENGINE_BROWSER_BASE_URL` to run against an already-started local or remote instance instead of letting the script start one. For Basic Auth deployments, set `REGENGINE_BROWSER_USERNAME` and `REGENGINE_BROWSER_PASSWORD`; set `REGENGINE_BROWSER_TENANT` to send `X-RegEngine-Tenant` for an isolated smoke tenant. The script also accepts the equivalent `REGENGINE_REMOTE_*` variables used by `scripts/remote_smoke.py`. Set `REGENGINE_BROWSER_EXPECTED_BUILD_SHA` to fail fast when the instance under test is not running the expected commit — the smoke checks it against `/api/healthz` before driving any flow, so a stale deploy fails with a build-mismatch message instead of a confusing assertion further in. It falls back to `REGENGINE_EXPECTED_BUILD_SHA` when unset, so the two smokes can share one variable; `.github/workflows/remote-browser-smoke.yml` passes the workflow's own `github.sha`.

## Release smoke regression

Run the release smoke harness before tagging or handing the simulator to a design partner:

```bash
uv run python scripts/smoke_regression.py
```

The smoke harness uses FastAPI's in-process `TestClient` to exercise the operator-critical path: tenant-scoped fixture load, lineage lookup, FDA export, EPCIS export, scenario save/load, replay, and tenant isolation. If Basic Auth env vars are set, it sends matching Basic credentials automatically. Temporary smoke tenants are cleaned up after the run.

For a deployed shared-demo instance, run the remote smoke harness against the public HTTPS URL:

```bash
export REGENGINE_REMOTE_BASE_URL=https://regengine-inflow-lab-gh-production.up.railway.app
export REGENGINE_REMOTE_USERNAME=demo
export REGENGINE_REMOTE_PASSWORD='replace-with-shared-demo-password'
export REGENGINE_REMOTE_TENANT=remote-smoke
uv run --no-dev python scripts/remote_smoke.py
```

`scripts/remote_smoke.py` uses `httpx` with normal TLS verification to check `/api/healthz`, Basic Auth enforcement, credentialed CORS allow/block behavior, mock fixture loading, transformed-lot lineage, FDA CSV export, and EPCIS JSON-LD export. The tenant defaults to `remote-smoke`, fixture delivery stays in `mock` mode, and failure messages redact configured passwords and credential-like environment values. Set `REGENGINE_EXPECTED_BUILD_SHA` to fail fast when a deployed instance is not running the expected commit.

GitHub also has manual and nightly **Remote Smoke** and **Remote Browser Smoke** workflows for deployed demo validation. Configure repository secrets `REGENGINE_REMOTE_USERNAME` and `REGENGINE_REMOTE_PASSWORD`, then run `.github/workflows/remote-smoke.yml` for API/export checks or `.github/workflows/remote-browser-smoke.yml` for authenticated dashboard checks with optional `base_url` and `tenant` inputs. Scheduled runs target the Railway shared-demo URL with dedicated nightly tenants and compare `/api/healthz` build metadata to the workflow commit.

Use `RELEASE_CHECKLIST.md` as the full demo-ready gate. Use `DESIGN_PARTNER_DEMO_SCRIPT.md` for the call flow, expected talking points, fixture reset commands, and recovery steps.

## Full FSMA simulation

Run the one-command golden path demo:

```bash
python3 run_full_fsma_simulation.py
```

The script uses the in-process FastAPI app and the deterministic fresh-cut transformation fixture. It prints:

- events generated
- mock ingestion accepted count
- simulator KDE validation results
- lot lineage count
- FDA lot-trace CSV rows
- EPCIS event count

This is the shortest proof path for:

```text
simulate -> ingest -> validate -> trace -> export
```

## Delivery modes

The simulator supports three delivery modes, configured via the `delivery.mode` field:

### `mock` (default)
No credentials required. Events go to the built-in RegEngine stand-in, which **mirrors the live webhook's validation** rather than accepting everything: strict per-CTE KDE checks (exact key lookup, no aliasing), the location-identifier requirement, a 500-event batch cap, an empty-batch rejection, in-batch duplicate rejection, future-timestamp rejection, and 24-hour idempotency replays. When `REGENGINE_WEBHOOK_HMAC_SECRET` is set, the stand-in also **verifies** `X-Webhook-Signature` over the raw request bytes and 401s a missing, malformed or mismatched signature — so a signing bug fails here rather than only against production. Verification applies to the HTTP route; the in-process delivery path has no wire bytes to check and skips it. The route additionally accepts a mock-only `X-Mock-Friction` header (comma-separated `invalid_key`, `subscription_inactive`, `rate_limit`) so an external caller can rehearse the same failure modes the console's toggles inject. Accepted events return a synthetic `event_id`, `sha256_hash`, and `chain_hash`; rejected events return the same per-event `errors` shape live RegEngine produces. Safe for demos and design-partner testing.

Mock delivery also supports **failure-mode rehearsal** via `delivery.mock_friction` (or the "Rehearse failure modes" toggles in the console): `invalid_key` (401), `subscription_inactive` (402), and `rate_limit` (429) inject the exact failures a live integration hits, so operators can practice diagnosing and retrying — retries reuse the stored idempotency key, so nothing double-ingests.

### `live`
Sends real traffic to a RegEngine workspace. Configure from the dashboard or via the API with:

- `api_key`
- `tenant_id`
- Optional `endpoint` override (defaults to `https://www.regengine.co/api/v1/webhooks/ingest`)

For controlled live workspace validation, use `scripts/live_trial.py`. It refuses to send live traffic unless `--confirm-live` is supplied, always performs a mock dry-run first, and sends exactly one live batch before stopping.

### `none`
Generates and persists events locally without delivering them anywhere. Useful for seeding fixtures.

Every stored record tracks `delivery_status`, `destination_mode`, `delivery_attempts`, last delivery timestamps, and non-secret `delivery_metadata` such as delivery mode, attempted event count, live endpoint host/path, HTTP status, and idempotency key. The dashboard delivery monitor summarizes posted, failed, generated-only, and retryable records — with recovery guidance per failure class — and per-event **rejections** from RegEngine (or the mock) are stored as failed records carrying the validator's errors. Failed records can be retried through the dashboard or `POST /api/delivery/retry` after switching to a working `mock` or `live` delivery configuration; retries reuse each record's stored idempotency key.

## RegEngine integration settings

The console treats RegEngine like any third-party integration a customer configures, backed by three endpoints:

- `GET /api/integration/status` — sanitized connection state: mode, endpoint host, whether an API key / tenant are configured, whether HMAC signing is enabled, and active `mock_friction` codes. Secrets are never returned.
- `POST /api/integration/configure` — partial update of `mode`, `endpoint`, `api_key`, `tenant_id`, and `mock_friction`; omitted fields keep their stored values so the settings page can switch modes without re-entering credentials.
- `POST /api/integration/test` — probes RegEngine with the configured (or request-supplied) credentials using the cheapest authenticated read (`GET /api/v1/webhooks/recent?limit=1`) and maps the response to an actionable verdict: `connected`, `contract_mismatch` (authenticated read succeeded but the two deploys advertise different ingest contract versions), `unauthorized` (401), `subscription_inactive` (402), `forbidden` (403), `tenant_mismatch` (404), `rate_limited` (429), `service_unavailable` (503), `unreachable`, or `not_configured`. In mock mode with no credentials it reports `mock` without touching the network.

## Customer journey harness

`scripts/customer_journey.py` replays the full "experience in the wild" against a real RegEngine stack — onboarding through evidence export:

```bash
# Local RegEngine (Postgres + Redis + monolith), provisions everything itself:
export REGENGINE_ADMIN_KEY=...            # the stack's ADMIN_MASTER_KEY
export REGENGINE_BASE_URL=http://localhost:8000
export REGENGINE_REDIS_URL=redis://localhost:6379/0
uv run python scripts/customer_journey.py --local
```

Steps: health probe → tenant + API key provisioning through `/v1/admin` → billing activation (seeds `billing:tenant:{id}` in Redis so the subscription gate passes) → connection test → several canonical CTE batches ingested with the same engine and live client the console uses → friction demos (a KDE-rejected event and an idempotency replay) → verification that RegEngine now holds the evidence (`/api/v1/webhooks/recent`, `/api/v1/webhooks/chain/verify`, `/api/v1/fda/export/all`) → teardown of the tenant and key it provisioned. The teardown runs from a `finally` block, so a run that fails partway still removes what it created; if the admin API refuses the delete, the journey reports that as a failed step naming the tenant id to remove by hand.

For a deployed RegEngine, the harness follows `scripts/live_trial.py`'s gating: it refuses to run without `--confirm-live`, requires pre-provisioned `REGENGINE_LIVE_ENDPOINT` / `REGENGINE_LIVE_API_KEY` / `REGENGINE_LIVE_TENANT_ID`, never provisions or touches Redis, sends one small batch, and skips the deliberate-failure demos.

## Basic auth and tenant storage

Basic Auth is opt-in so local mock demos remain frictionless. Set both environment variables to require credentials for the dashboard and API:

```bash
export REGENGINE_BASIC_AUTH_USERNAME=demo
export REGENGINE_BASIC_AUTH_PASSWORD=change-me
```

When Basic Auth is enabled, requests without valid credentials receive `401` with a `WWW-Authenticate` challenge. If no tenant header is supplied, the authenticated username becomes the tenant id.

Use `X-RegEngine-Tenant` to select an explicit tenant scope. Tenant ids must be 1-64 characters and can contain only letters, numbers, dots, underscores, or hyphens. Tenant-scoped controllers keep separate simulator state, event logs, mock ingest responses, scenario saves, lineage, and exports under `data/tenants/{tenant_id}/`.

`GET /api/health` and the dashboard stats area expose the active tenant id, whether Basic Auth is enabled, and whether storage is local default or tenant-scoped. Passwords, API keys, and other credentials are never returned; live delivery status preserves the active mode and endpoint while redacting the RegEngine API key and tenant id.

Credentialed browser requests are limited to explicit CORS origins. By default the app allows the local dashboard origins `http://127.0.0.1:8000` and `http://localhost:8000`. For shared demos, set comma-separated HTTPS origins:

```bash
export REGENGINE_CORS_ORIGINS=https://demo.example.com,https://partner-demo.example.com
```

Wildcard CORS origins are rejected because Basic Auth and tenant-scoped requests may carry credentials. When Basic Auth is enabled, state-changing browser requests such as simulator start, step, reset, fixture load, import, replay, retry, and scenario save/load must present a trusted `Origin` or `Referer` from `REGENGINE_CORS_ORIGINS`; command-line and server-to-server calls without browser origin headers continue to work with valid Basic credentials.

Protected tenant operations are available when Basic Auth is enabled:

- `GET /api/operator/tenants` lists cached and on-disk tenant scopes, record counts, scenario-save counts, and storage paths.
- `POST /api/operator/tenants/{tenant_id}/reset` stops that tenant's loop and clears its event log while preserving the tenant directory and scenario saves.
- `DELETE /api/operator/tenants/{tenant_id}` stops that tenant's loop, evicts its cached controller, and deletes its tenant data directory.

These endpoints reject unauthenticated requests and reject the default local tenant so the unprotected local demo surface stays simple.

## Replay mode

Replay mode reads previously persisted `StoredEventRecord` JSONL lines, rebuilds the RegEngine ingest payload as:

```json
{
  "source": "codex-simulator",
  "events": [
    {
      "cte_type": "receiving",
      "traceability_lot_code": "TLC-20260421-000003",
      "product_description": "Romaine Lettuce",
      "quantity": 500,
      "unit_of_measure": "cases",
      "location_name": "Distribution Center #4",
      "timestamp": "2026-02-05T08:30:00Z",
      "kdes": {}
    }
  ]
}
```

By default, `POST /api/simulate/replay` uses the current `config.persist_path`, `config.source`, and `config.delivery`. You can override the JSONL path, source, or delivery mode in the request body. Delivery still uses the same `mock`, `live`, and `none` branches as normal generation.

Replay responses include `status`, `read`, `replayed`, `posted`, `failed`, `source`, `persist_path`, `delivery_mode`, `delivery_attempts`, and any delivery `response` or `error`. Replay does not create new stored records.

## CSV import

`POST /api/import/csv` accepts CSV text and imports either scheduled RegEngine-shaped events or seed lots. Valid rows are delivered through the selected delivery mode and persisted as `StoredEventRecord` JSONL entries. Invalid rows are skipped, with deterministic row-level errors in the response. Accepted rows are also checked against CTE-specific KDE expectations and can return warnings for missing lineage, document, location, or date context. The default dashboard/API delivery remains **`mock`** unless you explicitly submit a different `delivery` object.

Request body:

```json
{
  "import_type": "scheduled_events",
  "csv_text": "cte_type,traceability_lot_code,...",
  "source": "codex-simulator",
  "delivery": {
    "mode": "mock"
  }
}
```

For `scheduled_events`, each row must include the current RegEngine event fields:

```text
cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp
```

Optional `kdes` may be a JSON object. Additional non-empty columns are imported as KDEs, so columns such as `source_traceability_lot_code`, `input_traceability_lot_codes`, `reference_document_type`, and `reference_document_number` preserve lineage and FDA-export context. `parent_lot_codes` is optional and can be a JSON array or a `|`, `;`, or comma-separated list.

For `seed_lots`, each row must include:

```text
traceability_lot_code,product_description,quantity,unit_of_measure,location_name
```

Seed lots become valid `harvesting` events. Optional `timestamp`, `harvest_date`, `field_name`, `immediate_subsequent_recipient`, reference document columns, `kdes` JSON, and other KDE columns are preserved. If no timestamp is supplied, the import time is used.

Import responses include `status`, `total`, `accepted`, `rejected`, `stored`, `posted`, `failed`, `delivery_attempts`, `lot_codes`, `errors[]`, and `warnings[]` with row number, field, and message. Warnings are advisory and do not change the RegEngine ingest payload shape.

## Scenario presets

Use `config.scenario` to pick a deterministic product/location/flow mix without changing the RegEngine ingest payload shape. Supported values:

| Scenario | Value | Demo emphasis |
|---|---|---|
| Leafy greens supplier | `leafy_greens_supplier` | Farm-origin leafy greens through cooling, packout, and outbound cold chain |
| Fresh-cut processor | `fresh_cut_processor` | Ingredient lots routed into processor inventory and transformed into fresh-cut outputs |
| Retailer readiness demo | `retailer_readiness_demo` | Retail-ready cases moving quickly through DC receiving and store-level receipts |
| Seafood first receiver | `seafood_first_receiver` | Vessel-linked first land-based receiving with dockside handoff continuity |
| Dairy continuous flow | `dairy_continuous_flow` | Continuous silo/vat movement without produce-style cooling records |
| Co-packer (nut butters) | `copacker_nut_butter` | Contract manufacturer transforming grower-partner nut lots into branded and private-label nut butters |
| Broadline distributor | `broadline_distributor` | Pure ship/receive wholesale traceability between grower-shippers and retail/foodservice — no transformation |
| Foodservice restaurant group | `foodservice_restaurant_group` | Central commissary prepping RTE deli salads for restaurant locations |
| Shell egg producer-packer | `shell_egg_producer` | Layer houses through egg cooling, grading/packing, and carton distribution — no transformation |

Together the presets cover the FSMA 204 covered-entity spectrum — farms, coolers, initial packers, first land-based receivers, processors, co-packers, distributors, retail, foodservice, and shell eggs — anyone who manufactures, processes, packs, or holds FTL foods. Scenario selection is available in the dashboard, in `SimulationConfig`, and via `GET /api/scenarios`. The default is `leafy_greens_supplier`, and delivery still defaults to **`mock`**.

**Operation size.** Every line profile composes with `config.scale` (the "Operation size" selector in the console): `small` runs a single-farm producer — one facility per tier, two retail destinations, half the lots in flight, quarter-size lots, mostly direct shipments with rare transformation; `midsize` (default) is the preset as authored; `enterprise` expands each tier into a multi-site network (12 farms, 4 coolers/packers, 3 processors, 5 DCs, 10 retailers — cloned sites with fresh, valid GLNs) with 4× lots in flight and 4× lot volumes, so commingling and multi-DC routing dominate. Scaling is deterministic (no RNG in network generation), keeps every event RegEngine-canonical, and only shapes the data — the wire contract is untouched. The journey harness accepts `--scale small|midsize|enterprise`.

Per-scenario save/load stores one saved slot per scenario under `data/scenario_saves/`. A saved scenario includes the sanitized simulator config and the current stored event records, so operators can restore repeatable demo states after switching scenarios. Live API keys are never saved; live delivery settings are restored as mock delivery to preserve mock-first safety.

## Demo fixtures

Use `GET /api/demo-fixtures` to list deterministic demo playback fixtures. Each fixture contains fixed RegEngine-shaped events with stable timestamps, lot codes, reference documents, and parent-lot lineage. `POST /api/demo-fixtures/{fixture_id}/load` loads a fixture into the event store and optionally delivers it through `mock`, `live`, or `none` delivery.

Supported fixture IDs:

| Fixture | Value | Demo emphasis |
|---|---|---|
| Leafy greens trace | `leafy_greens_trace` | One leafy greens lot from harvest through DC receipt |
| Fresh-cut transformation | `fresh_cut_transformation` | Two ingredient lots transformed into one fresh-cut output lot |
| Retailer handoff | `retailer_handoff` | Retail-ready cases through DC and store receipts |

The dashboard fixture loader resets the current event log before loading the selected fixture so demos start from a known state.

## FDA export presets

`GET /api/mock/regengine/export/fda-request` still returns the same FDA request CSV (13 columns since the FSMA-required Ship-To / Previous Source and TLC Source Reference columns were added) and remains backward compatible with optional `start_date` and `end_date` filters. Date filters must be valid inclusive `YYYY-MM-DD` dates, and `start_date` must not be later than `end_date`. It now also accepts:

- `preset`: one of `all_records`, `lot_trace`, `shipment_handoff`, `receiving_log`, or `transformation_batches`
- `traceability_lot_code`: optional for most presets, required for `lot_trace`

If a lot code is supplied, the export is scoped to that lot's transitive lineage before applying the preset filter. `GET /api/mock/regengine/export/presets` returns the preset catalog used by the dashboard. The dashboard export panel builds CSV and EPCIS download links from the same lot and date filters.

## EPCIS 2.0 export scaffolding

`GET /api/mock/regengine/export/epcis` derives a scaffolded EPCIS 2.0 JSON-LD document from stored simulator records. This is intentionally additive: it does not change live RegEngine ingest payloads, the mock ingest route, or the FDA CSV export shape.

Supported query parameters:

- `start_date`: optional inclusive `YYYY-MM-DD`
- `end_date`: optional inclusive `YYYY-MM-DD`
- `traceability_lot_code`: optional lot code; when supplied, the export uses the same transitive lineage graph as `/api/lineage/{traceability_lot_code}`

Invalid date formats, impossible dates, and inverted ranges return `400` so operators catch export filter mistakes before sharing files.

The dashboard exposes a `Download EPCIS` control beside the FDA CSV export. It uses the same optional lot code and date filters as the CSV export panel, but does not apply FDA-only preset filters.

The export returns an `EPCISDocument` with `ObjectEvent` records for harvesting, cooling, packing, shipping, and receiving CTEs, plus `TransformationEvent` records for transformation CTEs. RegEngine-specific fields are preserved under the `regengine:` JSON-LD namespace so KDEs, parent lot codes, document references, product descriptions, and original CTE types remain visible while the current webhook contract stays unchanged.

## Design-partner demo script

`DESIGN_PARTNER_DEMO_SCRIPT.md` contains a repeatable design-partner walkthrough with pre-demo verification, talking points, expected dashboard states, lot codes to inspect, FDA/EPCIS export checks, reset commands, and recovery notes. The default path uses the deterministic `fresh_cut_transformation` fixture and keeps delivery in mock mode.

## Deployment profiles

`DEPLOYMENT_PROFILES.md` defines three operator profiles:

- Local demo: bind to `127.0.0.1`, no Basic Auth, default local storage, mock delivery.
- Shared demo: Basic Auth enabled, tenant-scoped storage, mock delivery, and HTTPS/proxy guidance.
- Live ingest trial: shared-demo protections plus an explicit one-batch live delivery workflow using the documented RegEngine endpoint.

The service wrapper examples below can be used with any profile; keep the profile's bind address, auth, tenant, and delivery safeguards intact.

## API reference

### Simulator control

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Authenticated liveness probe, public build metadata, tenant/auth context, and current config snapshot |
| `GET` | `/api/healthz` | Unauthenticated platform/container healthcheck with public build metadata |
| `GET` | `/api/scenarios` | List available scenario presets |
| `GET` | `/api/scenario-saves` | List saved per-scenario demo states |
| `POST` | `/api/scenario-saves/{scenario_id}` | Save the current or supplied config and event log for a scenario |
| `POST` | `/api/scenario-saves/{scenario_id}/load` | Restore a saved scenario config and event log |
| `GET` | `/api/demo-fixtures` | List deterministic demo playback fixtures |
| `POST` | `/api/demo-fixtures/{fixture_id}/load` | Load a deterministic fixture into the event store |
| `GET` | `/api/simulate/status` | Running state, config, and aggregate stats |
| `GET` | `/api/operator/tenants` | List protected tenant scopes and storage counts |
| `POST` | `/api/operator/tenants/{tenant_id}/reset` | Reset one protected tenant event log |
| `DELETE` | `/api/operator/tenants/{tenant_id}` | Delete one protected tenant data directory |
| `POST` | `/api/simulate/start` | Start the loop (accepts a `config` body) |
| `POST` | `/api/simulate/stop` | Stop the loop |
| `POST` | `/api/simulate/step` | Emit one batch synchronously |
| `POST` | `/api/simulate/replay` | Replay persisted JSONL events through the configured delivery mode |
| `POST` | `/api/simulate/reset` | Clear state and persisted events |
| `GET` | `/api/simulate/stream` | Server-Sent Events snapshots for live dashboard updates |
| `POST` | `/api/import/csv` | Bulk import scheduled events or seed lots from CSV text |
| `POST` | `/api/delivery/retry` | Retry failed stored deliveries with the current or supplied delivery config |

All routes accept optional `X-RegEngine-Tenant` for tenant-scoped storage. If Basic Auth is enabled, include standard HTTP Basic credentials.

### Inspection

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/events` | List persisted events |
| `GET` | `/api/lineage/{traceability_lot_code}` | Full lineage graph for a lot |

### RegEngine integration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/integration/status` | Sanitized connection state (mode, endpoint host, key/tenant configured, HMAC, friction) |
| `POST` | `/api/integration/configure` | Partial update of mode/endpoint/api_key/tenant_id/mock_friction |
| `POST` | `/api/integration/test` | Probe RegEngine with configured or supplied credentials; returns an actionable verdict |

### Mock RegEngine

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/mock/regengine/ingest` | RegEngine-shaped ingest mirroring the live webhook's validation (per-event accept/reject) |
| `GET` | `/api/mock/regengine/export/presets` | List FDA request export presets |
| `GET` | `/api/mock/regengine/export/fda-request` | Mock 13-column FDA request CSV |
| `GET` | `/api/mock/regengine/export/epcis` | Scaffolded EPCIS 2.0 JSON-LD export |

### Example: start the simulator in live mode

```bash
curl -X POST http://127.0.0.1:8000/api/simulate/start \
  -H 'Content-Type: application/json' \
  -d '{
    "config": {
      "source": "codex-simulator",
      "scenario": "fresh_cut_processor",
      "interval_seconds": 1.0,
      "batch_size": 3,
      "seed": 204,
      "persist_path": "data/events.jsonl",
      "delivery": {
        "mode": "live",
        "endpoint": "https://www.regengine.co/api/v1/webhooks/ingest",
        "api_key": "YOUR_API_KEY",
        "tenant_id": "YOUR_TENANT_UUID"
      }
    }
  }'
```

### Example: reset into a retailer readiness scenario

```bash
curl -X POST http://127.0.0.1:8000/api/simulate/reset \
  -H 'Content-Type: application/json' \
  -d '{
    "scenario": "retailer_readiness_demo",
    "batch_size": 3,
    "seed": 204,
    "persist_path": "data/events.jsonl"
  }'
curl -X POST http://127.0.0.1:8000/api/simulate/step
```

### Example: step once and inspect events

```bash
curl -X POST http://127.0.0.1:8000/api/simulate/step
curl http://127.0.0.1:8000/api/events
```

### Example: load a deterministic fresh-cut demo fixture

```bash
curl -X POST http://127.0.0.1:8000/api/demo-fixtures/fresh_cut_transformation/load \
  -H 'Content-Type: application/json' \
  -d '{
    "reset": true,
    "delivery": {
      "mode": "mock"
    }
  }'
```

### Example: save and reload a scenario state

```bash
curl -X POST http://127.0.0.1:8000/api/scenario-saves/fresh_cut_processor
curl -X POST http://127.0.0.1:8000/api/scenario-saves/fresh_cut_processor/load
```

### Example: replay the current persisted log

```bash
curl -X POST http://127.0.0.1:8000/api/simulate/replay
```

### Example: replay another JSONL file without delivery

```bash
curl -X POST http://127.0.0.1:8000/api/simulate/replay \
  -H 'Content-Type: application/json' \
  -d '{
    "persist_path": "data/events.jsonl",
    "source": "codex-simulator",
    "delivery": {
      "mode": "none"
    }
  }'
```

### Example: subscribe to live dashboard updates

```bash
curl -N http://127.0.0.1:8000/api/simulate/stream
```

Each SSE `snapshot` includes a monotonic `revision`, the same status payload returned by `/api/simulate/status`, and recent event records from `/api/events`. Use `limit` to control the number of recent events and `once=true` for a one-shot smoke check.

### Example: retry failed deliveries in mock mode

```bash
curl -X POST http://127.0.0.1:8000/api/delivery/retry \
  -H 'Content-Type: application/json' \
  -d '{
    "delivery": {
      "mode": "mock"
    }
  }'
```

### Example: trace a lot

```bash
curl http://127.0.0.1:8000/api/lineage/TLC-20260421-000003
```

The lineage response keeps the original `records[]` event timeline and adds `nodes[]` plus `edges[]` so transformed outputs can be displayed as a lot graph. `nodes[]` summarizes each related lot, and `edges[]` links source/input lot codes to downstream packed or transformed lots.

### Example: export a lot-trace FDA request slice

```bash
curl "http://127.0.0.1:8000/api/mock/regengine/export/fda-request?preset=lot_trace&traceability_lot_code=TLC-20260421-000003"
```

### Example: export a lot-trace EPCIS scaffold

```bash
curl "http://127.0.0.1:8000/api/mock/regengine/export/epcis?traceability_lot_code=TLC-20260421-000003"
```

## RegEngine payload contract

The live delivery client targets the current RegEngine webhook shape:

- **Endpoint:** `https://www.regengine.co/api/v1/webhooks/ingest`
- **Headers:** `X-RegEngine-API-Key`, `X-Tenant-ID`, `Idempotency-Key` (required by RegEngine; generated per batch and reused on retry), `Content-Type: application/json`, and `X-Webhook-Signature: sha256=<hex>` when `REGENGINE_WEBHOOK_HMAC_SECRET` is set (HMAC-SHA256 over the exact request body bytes)
- **Payload** (note the canonical KDEs — RegEngine's validator uses strict string lookup, so split fields like `reference_document_type`/`_number` do **not** satisfy `reference_document`):

```json
{
  "source": "erp",
  "events": [
    {
      "cte_type": "receiving",
      "traceability_lot_code": "00012345678901-LOT-2026-001",
      "product_description": "Romaine Lettuce",
      "quantity": 500,
      "unit_of_measure": "cases",
      "location_name": "Distribution Center #4",
      "timestamp": "2026-02-05T08:30:00Z",
      "kdes": {
        "receive_date": "2026-02-05",
        "receiving_location": "Distribution Center #4",
        "ship_from_location": "Valley Fresh Farms",
        "immediate_previous_source": "Valley Fresh Farms",
        "reference_document": "Bill of Lading BOL-2026-001",
        "tlc_source_reference": "SRC-2026-001"
      }
    }
  ]
}
```

Required KDEs per CTE are mirrored in `app/cte_rules.py` and pinned to RegEngine's `REQUIRED_KDES_BY_CTE` by `tests/test_regengine_contract_pin.py`; the detailed contract reference lives in `.agents/skills/regengine-api-contract/references/contract.md`.

**Contract version handshake.** Both sides advertise an ingest contract version (`app/contract.py` here, `webhook_models.INFLOW_CONTRACT_VERSION` in RegEngine) — inflow-lab via `/api/healthz`, `/api/health`, and `/api/integration/status`; RegEngine via `/health`. The test-connection probe compares them and reports a `contract_mismatch` verdict when deployed instances have skewed (one side running an older deploy), so version drift is a visible, named state instead of a silent live-post failure. Bump the version in both repos together whenever the wire contract changes. The mock FDA export was RegEngine's documented 11-column request export shape; it now carries two additional columns (Ship-To / Previous Source Location Description, TLC Source Reference) that FSMA 204 requires for Shipping and Receiving but that shape omitted. **RegEngine's mirrored export needs the same two columns before the shapes match again.** The EPCIS 2.0 export is a separate derived JSON-LD scaffold and does not change this webhook contract.

**Contract pin staleness.** The KDE table in `app/cte_rules.py` is pinned against RegEngine's real contract, but nothing used to notice when the two drifted — the mock and the pin test could keep agreeing with each other while both disagreed with production. `app/contract.py` now carries two guards:

- `assert_contract_pin_is_fresh()` fails an ordinary pytest test once `CONTRACT_LAST_CONFIRMED` is older than 90 days, so a pin nobody has re-checked becomes a build failure rather than a silent assumption. Override the window with `REGENGINE_CONTRACT_STALENESS_WINDOW_DAYS`. This is a deliberate time bomb: it will eventually fail with nothing actually wrong, because "nobody has verified this lately" is the condition it reports. Re-confirm the pin against RegEngine and move the date — do not delete the check.
- `check_remote_kde_contract()` diffs `REQUIRED_KDES` against a machine-readable upstream artifact, loaded from `REGENGINE_CONTRACT_ARTIFACT_PATH` (local file) or `REGENGINE_CONTRACT_ARTIFACT_URL`. With neither set it raises rather than reporting a match, so an unverifiable check can never read as a passing one.

Run both from the CLI with `uv run python -m app.contract` — exit `0` clean, `1` stale pin, `2` real mismatch, `3` unverifiable. Wiring it into CI is deliberately **not** done yet: it needs RegEngine to publish `REQUIRED_KDES_BY_CTE` as a fetchable JSON artifact first, and until that exists a CI step would just pin the build permanently red on exit `3`. Once published, the natural home is the existing weekly `schedule` trigger in `.github/workflows/ci.yml` rather than every PR, since it depends on an external resource.

## Deployment

### macOS LaunchAgent (auto-start on login)

A LaunchAgent is the simplest way to keep the server running on a developer Mac. The agent starts the server on login and restarts it if it crashes.

1. Install dependencies as described in [Quick start](#quick-start-local-dev).
2. Create `~/Library/LaunchAgents/com.regengine.uvicorn.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.regengine.uvicorn</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/regengine_codex_workspace/.venv/bin/uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/YOU/regengine_codex_workspace</string>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <key>StandardOutPath</key>
  <string>/Users/YOU/regengine_codex_workspace/uvicorn.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOU/regengine_codex_workspace/uvicorn.err.log</string>
</dict>
</plist>
```

Replace `/Users/YOU` with your home directory. **Note:** keep the project outside of `~/Desktop`, `~/Documents`, or `~/Downloads`; macOS privacy (TCC) blocks launchd from reading those folders without Full Disk Access.

3. Load and verify:

```bash
launchctl load -w ~/Library/LaunchAgents/com.regengine.uvicorn.plist
launchctl list | grep com.regengine.uvicorn     # should show a numeric PID
curl http://127.0.0.1:8000/api/health
```

4. To stop or restart:

```bash
launchctl unload ~/Library/LaunchAgents/com.regengine.uvicorn.plist
launchctl load   ~/Library/LaunchAgents/com.regengine.uvicorn.plist
```

### Linux systemd unit

Create `/etc/systemd/system/regengine.service`:

```ini
[Unit]
Description=RegEngine Inflow Lab
After=network.target

[Service]
Type=simple
User=YOU
WorkingDirectory=/home/YOU/regengine_codex_workspace
ExecStart=/home/YOU/regengine_codex_workspace/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now regengine
sudo systemctl status regengine
journalctl -u regengine -f    # live logs
```

### Docker (optional)

The repository ships with a production-oriented `Dockerfile`. The entrypoint prepares the mounted data directory, drops to a non-root app user for Uvicorn, stores default simulator data under `/data`, and includes a healthcheck for `/api/healthz`.

```bash
docker build -t regengine-inflow-lab .
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/data:/data" \
  -e REGENGINE_BASIC_AUTH_USERNAME=demo \
  -e REGENGINE_BASIC_AUTH_PASSWORD=change-me \
  -e REGENGINE_CORS_ORIGINS=http://127.0.0.1:8000 \
  regengine-inflow-lab
```

`railway.json` uses the same Dockerfile and healthcheck for Railway deployments. Mount persistent storage at `/data` and keep `REGENGINE_DATA_DIR=/data`.

The shared demo deploys from Railway's GitHub connection, which injects `RAILWAY_GIT_COMMIT_SHA`, so its build identity needs no manual variable. Leave `REGENGINE_BUILD_SHA` and `REGENGINE_BUILD_BRANCH` **unset** there: they take precedence over the injected metadata (`app/build_info.py`), so setting them makes `/api/healthz` report a hand-typed commit rather than what is running, which is exactly the stale-demo blindness the nightly smoke exists to catch. Confirm with `build.commit_source: RAILWAY_GIT_COMMIT_SHA`.

#### Manual CLI deploy (fallback)

Only for a service deployed by hand with `railway up`. There is no GitHub connection injecting metadata, and `.dockerignore` excludes `.git`, so these variables are the container's only source of build identity:

```bash
railway variable set --skip-deploys REGENGINE_BUILD_SHA="$(git rev-parse HEAD)" \
  REGENGINE_BUILD_BRANCH="$(git branch --show-current)"
railway up --ci -m "Deploy $(git rev-parse --short HEAD)"
```

A stale value makes `/api/healthz` report a commit that is not deployed. If the service is later switched to Railway's GitHub integration, delete both variables again. `DEPLOYMENT_PROFILES.md` -> "Manual CLI deploy (fallback)" carries the same instruction, and `scripts/cutover_preflight.sh` fails a service whose `commit_source` is anything but `RAILWAY_GIT_COMMIT_SHA`.

The health responses always include `build.version`; `build.commit_sha`, `build.commit_sha_short`, `build.branch`, and `build.deployment_id` are populated from whitelisted environment variables when available, or from local `.git` metadata during local development.

## Logs and troubleshooting

Every HTTP request emits an application log line like:

```text
request method=POST path=/api/demo-fixtures/fresh_cut_transformation/load status=200 duration_ms=42.10 tenant=remote-smoke delivery_mode=mock
```

The request log intentionally excludes headers, credentials, query strings, request bodies, response bodies, and export contents. Use it to correlate route failures, tenant scope, and the active delivery mode without exposing Basic Auth passwords, API keys, live tenant ids, or downloaded FDA/EPCIS data.

| Location | What it contains |
|---|---|
| `uvicorn.out.log` | Server stdout (request logs, lifecycle messages) |
| `uvicorn.err.log` | Server stderr (Python tracebacks, startup errors) |
| `data/events.jsonl` | Persisted simulator events |
| `data/tenants/{tenant_id}/events.jsonl` | Tenant-scoped simulator events |
| `data/tenants/{tenant_id}/scenario_saves/` | Tenant-scoped saved scenario states |

Common checks:

```bash
# Is the service running?
launchctl list | grep com.regengine.uvicorn   # macOS
systemctl status regengine                    # Linux

# Health probe
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/healthz

# Tail logs (macOS)
tail -f ~/regengine_codex_workspace/uvicorn.err.log

# Railway logs
railway logs --lines 100
railway logs --http --status ">=400" --lines 50
```

Common failure patterns:

- Auth failures: request logs show `status=401` on `/api/...`; confirm `REGENGINE_BASIC_AUTH_USERNAME` and `REGENGINE_BASIC_AUTH_PASSWORD` are set as intended.
- CORS failures: Railway HTTP logs may show successful `OPTIONS` but the browser blocks a follow-up request; confirm `REGENGINE_CORS_ORIGINS` is the exact HTTPS dashboard origin.
- Volume/storage failures: `/api/health` should report tenant-scoped paths under `REGENGINE_DATA_DIR`; confirm Railway has a volume mounted at `/data` and `REGENGINE_DATA_DIR=/data`.
- Stale deployment failures: `/api/healthz` should report the expected `build.commit_sha_short` **and** `build.commit_source: RAILWAY_GIT_COMMIT_SHA`. If `commit_source` is `REGENGINE_BUILD_SHA`, that variable is masking the real commit on the GitHub-connected service — delete it (and `REGENGINE_BUILD_BRANCH`) rather than refreshing it. If the source is already `RAILWAY_GIT_COMMIT_SHA` and the SHA is still behind `main`, the deploy itself did not land: redeploy current `main`.
- Live delivery failures: request logs identify the route and tenant while dashboard delivery stats show the sanitized delivery error; confirm endpoint, API key, and tenant id before retrying.
- Local dependency conflicts: remove `.venv`, run `uv sync --group dev`, and retry before diagnosing app failures; global Python packages such as OpenTelemetry or Semgrep can drift independently of this repo.
- Railway startup log noise: Uvicorn startup messages may appear with `level=error` in Railway logs. Treat them as noise unless there is a traceback, failed deployment, or HTTP 5xx.

If the health check fails before request logs appear, the first place to look is `uvicorn.err.log` or `railway logs --deployment` for a Python traceback or startup error.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution workflow.
Commits require DCO sign-off (`git commit -s`) — see the
[Developer Certificate of Origin (DCO)](CONTRIBUTING.md#developer-certificate-of-origin-dco)
section.

Before touching code, read:

- `AGENTS.md` — repository operating agreements
- `.agents/skills/regengine-api-contract/SKILL.md` — payload contract details
- `AUTOPILOT_TASKS.md` — prioritized backlog

House rules in short:

- Keep the live ingest payload compatible with the RegEngine contract.
- Preserve **mock mode** as the default.
- Maintain lot lineage across CTEs.
- Run `uv run pytest` after any Python change.
- Prefer small, composable modules and deterministic tests.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Inflow Lab is deliberately permissive, unlike the main RegEngine engine: it is
adoption infrastructure. Integrators, ERP vendors, and design partners are
encouraged to copy the payload contract, reuse the client patterns (idempotency,
HMAC signing, retry semantics), and fork the simulator for their own testing.

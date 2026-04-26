# RegEngine Inflow Lab

A mock-first FSMA 204 traceability simulator that emits **RegEngine-compatible ingest payloads** into a realistic supply-chain lifecycle. Ships with a FastAPI backend, a lightweight dashboard, and a built-in mock RegEngine endpoint for safe local testing.

## Table of contents

- [What it does](#what-it-does)
- [Project layout](#project-layout)
- [Quick start (local dev)](#quick-start-local-dev)
- [Running tests](#running-tests)
- [Delivery modes](#delivery-modes)
- [Replay mode](#replay-mode)
- [CSV import](#csv-import)
- [Scenario presets](#scenario-presets)
- [Demo fixtures](#demo-fixtures)
- [FDA export presets](#fda-export-presets)
- [EPCIS 2.0 export scaffolding](#epcis-20-export-scaffolding)
- [API reference](#api-reference)
- [RegEngine payload contract](#regengine-payload-contract)
- [Deployment](#deployment)
  - [macOS LaunchAgent (auto-start on login)](#macos-launchagent-auto-start-on-login)
  - [Linux systemd unit](#linux-systemd-unit)
  - [Docker (optional)](#docker-optional)
- [Logs and troubleshooting](#logs-and-troubleshooting)
- [Contributing](#contributing)

## What it does

The generator walks lots through a realistic supply-chain lifecycle so the resulting trace feels legitimate rather than random:

1. **Harvesting** originates at farms
2. **Cooling** moves harvested lots through cooler facilities
3. **Initial packing** creates downstream packed lots
4. **Shipping** creates a believable destination and reference document
5. **Receiving** corresponds to an actual prior shipment
6. **Transformation** consumes input lots and emits a new output lot
7. **Downstream shipping + receiving** moves transformed lots to DCs and retail

Each event is persisted with `event_id`, `sha256_hash`, and `chain_hash` so the flow feels production-like, and you can trace transitive lot lineage forward and backward through the dashboard or API.

## Project layout

```text
app/
  controller.py          # Simulator lifecycle (start/stop/step/reset)
  demo_fixtures.py       # Deterministic demo playback fixtures
  engine.py              # CTE generation and lot lineage logic
  epcis_export.py        # EPCIS 2.0 JSON-LD export scaffolding
  fda_export.py          # FDA-request CSV export presets and rendering
  main.py                # FastAPI app and route wiring
  mock_service.py        # Built-in mock RegEngine ingest endpoint
  models.py              # Pydantic models for config, events, payloads
  regengine_client.py    # HTTP client for live RegEngine delivery
  scenario_saves.py      # Per-scenario saved config and event-log snapshots
  scenarios.py           # Named scenario presets for product/location/flow mixes
  store.py               # Event persistence (JSONL)
  static/                # Dashboard (vanilla JS, HTML, CSS)
.agents/skills/regengine-api-contract/
.github/
  codex/prompts/autobuild.md
  workflows/ci.yml
  workflows/codex-autopilot.yml
tests/
AGENTS.md                # Repository instructions for Codex-style agents
AUTOPILOT_TASKS.md       # Standing backlog for unattended runs
PROMPT_FOR_CODEX.md      # Paste-ready Codex task prompt
pyproject.toml
requirements.txt
```

## Quick start (local dev)

Requires **Python 3.11+**.

```bash
# From the project root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the dev server (auto-reload)
uvicorn app.main:app --reload
```

Then open:

```
http://127.0.0.1:8000
```

The dashboard lets you choose a scenario preset, save/load per-scenario demo states, load deterministic demo fixtures, start/stop/step/reset the simulator, replay the current persisted event log, import CSV seed lots or scheduled events, inspect recent events, trace lot lineage, and export mock FDA request CSV presets. API users can also derive scaffolded EPCIS 2.0 JSON-LD exports from the same stored records. It subscribes to live status/event snapshots with Server-Sent Events and falls back to refresh polling if the stream disconnects. Delivery mode defaults to **`mock`** so no credentials are required.

Event records are stored as JSONL at `config.persist_path` (`data/events.jsonl` by default). Existing records at that path are loaded when the app starts or when a start/reset request points at a different path; reset clears the currently configured event log. Replay reads the JSONL log without appending, duplicating, or rewriting stored events.

## Running tests

```bash
pytest
```

The suite covers payload shape, engine determinism, and the HTTP API contract.

## Delivery modes

The simulator supports three delivery modes, configured via the `delivery.mode` field:

### `mock` (default)
No credentials required. Events are accepted by the built-in mock ingest service and returned with a synthetic `event_id`, `sha256_hash`, and `chain_hash`. Safe for demos and design-partner testing.

### `live`
Sends real traffic to a RegEngine workspace. Configure from the dashboard or via the API with:

- `api_key`
- `tenant_id`
- Optional `endpoint` override (defaults to `https://www.regengine.co/api/v1/webhooks/ingest`)

### `none`
Generates and persists events locally without delivering them anywhere. Useful for seeding fixtures.

Every stored record tracks `delivery_status`, `destination_mode`, `delivery_attempts`, and last delivery timestamps. The dashboard delivery monitor summarizes posted, failed, generated-only, and retryable records. Failed records can be retried through the dashboard or `POST /api/delivery/retry` after switching to a working `mock` or `live` delivery configuration.

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

`POST /api/import/csv` accepts CSV text and imports either scheduled RegEngine-shaped events or seed lots. Valid rows are delivered through the selected delivery mode and persisted as `StoredEventRecord` JSONL entries. Invalid rows are skipped, with deterministic row-level errors in the response. The default dashboard/API delivery remains **`mock`** unless you explicitly submit a different `delivery` object.

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

Import responses include `status`, `total`, `accepted`, `rejected`, `stored`, `posted`, `failed`, `delivery_attempts`, `lot_codes`, and `errors[]` with row number, field, and message.

## Scenario presets

Use `config.scenario` to pick a deterministic product/location/flow mix without changing the RegEngine ingest payload shape. Supported values:

| Scenario | Value | Demo emphasis |
|---|---|---|
| Leafy greens supplier | `leafy_greens_supplier` | Farm-origin leafy greens through cooling, packout, and outbound cold chain |
| Fresh-cut processor | `fresh_cut_processor` | Ingredient lots routed into processor inventory and transformed into fresh-cut outputs |
| Retailer readiness demo | `retailer_readiness_demo` | Retail-ready cases moving quickly through DC receiving and store-level receipts |

Scenario selection is available in the dashboard, in `SimulationConfig`, and via `GET /api/scenarios`. The default is `leafy_greens_supplier`, and delivery still defaults to **`mock`**.

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

`GET /api/mock/regengine/export/fda-request` still returns the same 11-column FDA request CSV and remains backward compatible with optional `start_date` and `end_date` filters. It now also accepts:

- `preset`: one of `all_records`, `lot_trace`, `shipment_handoff`, `receiving_log`, or `transformation_batches`
- `traceability_lot_code`: optional for most presets, required for `lot_trace`

If a lot code is supplied, the export is scoped to that lot's transitive lineage before applying the preset filter. `GET /api/mock/regengine/export/presets` returns the preset catalog used by the dashboard.

## EPCIS 2.0 export scaffolding

`GET /api/mock/regengine/export/epcis` derives a scaffolded EPCIS 2.0 JSON-LD document from stored simulator records. This is intentionally additive: it does not change live RegEngine ingest payloads, the mock ingest route, or the FDA CSV export shape.

Supported query parameters:

- `start_date`: optional inclusive `YYYY-MM-DD`
- `end_date`: optional inclusive `YYYY-MM-DD`
- `traceability_lot_code`: optional lot code; when supplied, the export uses the same transitive lineage graph as `/api/lineage/{traceability_lot_code}`

The export returns an `EPCISDocument` with `ObjectEvent` records for harvesting, cooling, packing, shipping, and receiving CTEs, plus `TransformationEvent` records for transformation CTEs. RegEngine-specific fields are preserved under the `regengine:` JSON-LD namespace so KDEs, parent lot codes, document references, product descriptions, and original CTE types remain visible while the current webhook contract stays unchanged.

## API reference

### Simulator control

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness probe + current config snapshot |
| `GET` | `/api/scenarios` | List available scenario presets |
| `GET` | `/api/scenario-saves` | List saved per-scenario demo states |
| `POST` | `/api/scenario-saves/{scenario_id}` | Save the current or supplied config and event log for a scenario |
| `POST` | `/api/scenario-saves/{scenario_id}/load` | Restore a saved scenario config and event log |
| `GET` | `/api/demo-fixtures` | List deterministic demo playback fixtures |
| `POST` | `/api/demo-fixtures/{fixture_id}/load` | Load a deterministic fixture into the event store |
| `GET` | `/api/simulate/status` | Running state, config, and aggregate stats |
| `POST` | `/api/simulate/start` | Start the loop (accepts a `config` body) |
| `POST` | `/api/simulate/stop` | Stop the loop |
| `POST` | `/api/simulate/step` | Emit one batch synchronously |
| `POST` | `/api/simulate/replay` | Replay persisted JSONL events through the configured delivery mode |
| `POST` | `/api/simulate/reset` | Clear state and persisted events |
| `GET` | `/api/simulate/stream` | Server-Sent Events snapshots for live dashboard updates |
| `POST` | `/api/import/csv` | Bulk import scheduled events or seed lots from CSV text |
| `POST` | `/api/delivery/retry` | Retry failed stored deliveries with the current or supplied delivery config |

### Inspection

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/events` | List persisted events |
| `GET` | `/api/lineage/{traceability_lot_code}` | Full lineage graph for a lot |

### Mock RegEngine

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/mock/regengine/ingest` | Accepts RegEngine-shaped payloads |
| `GET` | `/api/mock/regengine/export/presets` | List FDA request export presets |
| `GET` | `/api/mock/regengine/export/fda-request` | Mock 11-column FDA request CSV |
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
- **Headers:** `X-RegEngine-API-Key`, `X-Tenant-ID`, `Content-Type: application/json`
- **Payload:**

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
        "ship_from_location": "Valley Fresh Farms"
      }
    }
  ]
}
```

The mock FDA export mirrors RegEngine's documented 11-column request export shape. The EPCIS 2.0 export is a separate derived JSON-LD scaffold and does not change this webhook contract.

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

A minimal image is straightforward:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Build and run:

```bash
docker build -t regengine-inflow-lab .
docker run --rm -p 8000:8000 regengine-inflow-lab
```

## Logs and troubleshooting

| Location | What it contains |
|---|---|
| `uvicorn.out.log` | Server stdout (request logs, lifecycle messages) |
| `uvicorn.err.log` | Server stderr (Python tracebacks, startup errors) |
| `data/events.jsonl` | Persisted simulator events |

Common checks:

```bash
# Is the service running?
launchctl list | grep com.regengine.uvicorn   # macOS
systemctl status regengine                    # Linux

# Health probe
curl http://127.0.0.1:8000/api/health

# Tail logs (macOS)
tail -f ~/regengine_codex_workspace/uvicorn.err.log
```

If the health check fails, the first place to look is `uvicorn.err.log` for a Python traceback.

## Contributing

Before touching code, read:

- `AGENTS.md` — repository operating agreements
- `.agents/skills/regengine-api-contract/SKILL.md` — payload contract details
- `AUTOPILOT_TASKS.md` — prioritized backlog

House rules in short:

- Keep the live ingest payload compatible with the RegEngine contract.
- Preserve **mock mode** as the default.
- Maintain lot lineage across CTEs.
- Run `pytest` after any Python change.
- Prefer small, composable modules and deterministic tests.

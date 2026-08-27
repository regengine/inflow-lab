# RegEngine integration notes

Source of truth for what the Inflow Lab sends on the wire when
`delivery.mode=live`. Mirror of RegEngine's `services/ingestion/app/`
contract — keep these in sync when the live webhook contract changes.

## Endpoints

- Ingest endpoint: `POST /api/v1/webhooks/ingest`
- Export endpoint: `GET /v1/fsma/export/fda-request`
- Forward trace: `GET /v1/fsma/trace/forward/{tlc}`
- Backward trace: `GET /v1/fsma/trace/backward/{tlc}`

## Required headers on `POST /api/v1/webhooks/ingest`

| Header | Required | Source / format |
|---|---|---|
| `Content-Type: application/json` | Yes | Always |
| `X-RegEngine-API-Key` | Yes | Per-tenant API key issued by RegEngine |
| `X-Tenant-ID` | Recommended | RegEngine resolves tenant from body, then API-key lookup, then RBAC principal — but explicit is safer |
| `Idempotency-Key` | **Required** | UUID per logical request; RegEngine caches 2xx for 24h tenant-scoped |
| `X-Webhook-Signature: sha256=<hex>` | Required when `WEBHOOK_HMAC_SECRET` is set on RegEngine | HMAC-SHA256 over the raw request body bytes |

## Webhook HMAC signing

RegEngine's `_verify_webhook_signature` (services/ingestion/app/webhook_router_v2.py
lines 130–217) enforces HMAC-SHA256 when `WEBHOOK_HMAC_SECRET` is set
on the ingest service.

The simulator's `LiveRegEngineClient` reads `REGENGINE_WEBHOOK_HMAC_SECRET`
from the environment. When set:

- Body is JSON-serialized once with `separators=(",",":"), sort_keys=True`.
- Those exact bytes are sent via `httpx` `content=` (NOT `json=`) so the
  signed bytes equal the wire bytes.
- HMAC-SHA256 of body bytes is sent as `X-Webhook-Signature: sha256=<hex>`.

When unset, no signature header is sent and RegEngine's verifier no-ops.
This matches both sides' migration ramp.

**Production deployments must set the same secret on both sides.**

## CTE types accepted by RegEngine

RegEngine's `WebhookCTEType` (services/ingestion/app/webhook_models.py line 41):

- `growing` — legacy back-compat, normalizes to farm metadata. Simulator never emits.
- `harvesting`
- `cooling`
- `initial_packing`
- `first_land_based_receiving` — 21 CFR §1.1325, seafood / first-receiver flows.
  Simulator's `CTEType` enum includes this for hand-crafted fixture / CSV
  parity, but the default `LegitFlowEngine` does not emit it (no seafood
  scenario exists yet).
- `shipping`
- `receiving`
- `transformation`

## Per-event payload fields

```json
{
  "cte_type": "harvesting",
  "traceability_lot_code": "TLC-20260427-000001",
  "product_description": "Romaine Lettuce",
  "quantity": 500,
  "unit_of_measure": "cases",
  "location_name": "Valley Fresh Farms",
  "location_gln": "0850000001001",
  "timestamp": "2026-04-27T08:30:00Z",
  "kdes": { "...": "..." },
  "input_traceability_lot_codes": null
}
```

- `traceability_lot_code` — `min_length=3`, simulator emits `TLC-YYYYMMDD-NNNNNN`.
- `product_description` — `min_length=1, max_length=500`.
- `quantity` — `gt=0`.
- `unit_of_measure` — RegEngine logs but accepts unknown units; simulator
  uses values in the canonical valid set (`cases`, `lbs`, `kg`, `pallets`, etc.).
- `location_name` and `location_gln` — at least one required; if both absent,
  RegEngine's `require_location` validator looks for location-bearing KDEs
  (`ship_from_location`, `ship_to_location`, `receiving_location`, etc.).
  The simulator currently emits `location_name` only.
- `timestamp` — ISO 8601 string. Checked twice: a Pydantic field validator
  rejects anything more than `WEBHOOK_MAX_EVENT_FUTURE_HOURS` (24) in the
  future, and `_validate_event_timestamp_window`
  (`services/ingestion/app/webhook_router_v2/security.py`) **rejects**
  anything older than `WEBHOOK_MAX_EVENT_AGE_DAYS` (90) with "replay window
  exceeded". The floor is applied per event inside the route handler, so a
  stale timestamp rejects that one event and the rest of the batch still
  succeeds. Exactly 90 days old is inside the window — the comparison is
  `dt < now - timedelta(days=age_cap_days)`.

  This entry previously read "Older than 90 days accepted but flagged with
  `_historical_warning`". That was wrong in both halves: stale events are
  rejected, not accepted, and no `_historical_warning` symbol exists
  anywhere in RegEngine. It was hand-written in #43 and never re-checked —
  the same unfalsifiable-by-construction problem #104 built the generated
  KDE pin to solve. Corrected under #209 by reading
  `webhook_router_v2/security.py` and `routes.py` at the commit
  `tests/data/regengine_required_kdes.json` already pins, where
  RegEngine's own `test_replay_window_rejects_stale_event` asserts the
  rejection.
- `input_traceability_lot_codes` — Optional first-class field on RegEngine's
  `IngestEvent` for transformation CTEs. **RegEngine reads it from the
  top-level field, not from `kdes`.** This document previously claimed the
  opposite, and the simulator sent the value only inside `kdes`, so live
  ingest accepted every transformation event and silently dropped its
  input-lot lineage link (#91). The simulator now emits it top-level and
  keeps the `kdes` copy for the local validator, audit checks and exports.

## Required KDEs per CTE (RegEngine `REQUIRED_KDES_BY_CTE`)

KDE validation is **strict string lookup**. A typo or a split key (e.g.
`reference_document_type` instead of `reference_document`) causes the
event to be rejected with `Missing required KDE '<n>' for <cte> CTE`.

| CTE | Required KDEs (beyond top-level fields) |
|---|---|
| `harvesting` | `harvest_date`, `reference_document` (§1.1327(b)(5)) |
| `cooling` | `cooling_date`, `reference_document` (§1.1330(b)(6)) |
| `initial_packing` | `packing_date`, `reference_document` (§1.1335(c)(7)), `harvester_business_name` (§1.1335(c)(8)) |
| `first_land_based_receiving` | `landing_date`, `receiving_location`, `reference_document` (§1.1325(c)(7)) |
| `shipping` | `ship_date`, `ship_from_location`, `ship_to_location`, `reference_document` (§1.1340(c)(6)), `tlc_source_reference` (§1.1340(c)(7)) |
| `receiving` | `receive_date`, `receiving_location`, `immediate_previous_source` (§1.1345(c)(5)), `reference_document` (§1.1345(c)(6)), `tlc_source_reference` (§1.1345(c)(7)) |
| `transformation` | `transformation_date`, `reference_document` (§1.1350(c)(6)) |

**Top-level fields RegEngine treats as KDEs during validation:**
`traceability_lot_code`, `product_description`, `quantity`,
`unit_of_measure`, `location_name`, `location_gln`. These come from the
typed `IngestEvent` fields, not the `kdes` dict — but the simulator
satisfies them as typed fields too, so the distinction is invisible.

## Idempotency

`Idempotency-Key` is required (`webhook_router_v2.py` line 933,
`IdempotencyDependency(strict=True)`). The middleware caches 2xx
responses for 24h, scoped per tenant. The simulator generates a fresh
`uuid4().hex` for each new live delivery request and stores it in each record's
`delivery_metadata`.

Live delivery retries reuse the stored `delivery_metadata.idempotency_key`
when present and group failed records by `(source, idempotency_key)` so
RegEngine can identify the retry and return the cached 2xx response.
Records without prior idempotency metadata fall back to a fresh key.

## Mock export columns expected by this repo

(Used by the simulator's mock RegEngine endpoint for dashboard / FDA
preset rendering — does NOT affect live ingest.)

- Traceability Lot Code
- Event Type (CTE)
- Product Description
- Quantity
- Unit of Measure
- Location Description
- Location Identifier (GLN)
- Ship-To / Previous Source Location Description
- TLC Source Reference
- Date
- Time
- Reference Document Type
- Reference Document Number

Generated by `FDA_EXPORT_COLUMNS` in `app/fda_export.py` — that constant is
the source of truth; this list mirrors it. Two of these (Ship-To / Previous
Source, TLC Source Reference) were added for FSMA-required Shipping and
Receiving KDEs and are **not** in RegEngine's mirrored 11-column export yet,
so the two shapes are currently diverged. `Event Type (CTE)` was previously
mislabelled `Traceability Lot Code Description`, a column name FSMA 204 does
not define.

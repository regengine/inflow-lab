# RegEngine integration notes

What the Inflow Lab sends on the wire when `delivery.mode=live`, and what
RegEngine's `services/ingestion/app/` does with it.

## How much of this is verified, by what, and when

This file used to open by calling itself the "source of truth" and a
"mirror of RegEngine's contract". Nothing checked either claim, and
several statements below had gone stale against RegEngine's real source
(#211). Machinery to check some of it does exist (#104), so the claims are
now split by how strong they actually are.

**Checked on every `pytest` run.** `tests/test_contract_document.py`
parses the marked sections below and compares them against code and
generated data in this repository:

- the required-KDE table, against `tests/data/regengine_required_kdes.json`
  -- extracted from RegEngine's real `REQUIRED_KDES_BY_CTE` by
  `scripts/regengine_kde_contract.py`, checksummed, and provenance-pinned
  by `tests/test_contract_provenance.py`;
- the accepted CTE types, against that artifact and `app.schemas.domain.CTEType`;
- the ingest path and request headers, against `app/regengine_client.py`;
- the batch, replay-window and idempotency limits, against
  `app/mock_service.py`, whose constants exist to mirror them;
- the sample payload, against `app.schemas.domain.RegEngineEvent` and the
  GS1 check-digit helper in `app/engine.py`;
- the mock export column list, against `FDA_EXPORT_COLUMNS` in
  `app/fda_export.py`.

That proves this document agrees with this repository. It does not reach
RegEngine.

**Checked only against a RegEngine checkout.** The RegEngine file paths
and symbol names this document names are verified by
`tests/test_contract_document.py` only when `REGENGINE_CHECKOUT` points at
one:

```
REGENGINE_CHECKOUT=/path/to/regengine pytest tests/test_contract_document.py
```

No CI job here sets that variable, so those tests skip in ordinary runs.

**Checked only in the cross-repo CI job.** That the KDE artifact still
equals RegEngine's live source is re-established by the
`regengine-contract` job in `.github/workflows/ci.yml`, which has both
repositories on disk. It runs only when a change touches the
ingest-contract surface **and** the repository has cross-repo read access
configured; it never runs on a pull request from a fork. Between those
runs the artifact is a snapshot at a recorded commit, not a live mirror.

**Not checked by anything.** Everything else here is prose: header
semantics, RegEngine's tenant-resolution order, the export and trace
endpoints, and the CFR citations. Those were last confirmed by hand
against `regengine/regengine` @ `fa614d4d9d90261116cc17636bf98173c7d057c4`
on 2026-08-27.

## Endpoints

The ingest endpoint is the only one on the simulator's live path and the
only one anything here verifies:

- Ingest endpoint: `POST /api/v1/webhooks/ingest`

It is pinned to `DEFAULT_LIVE_INGEST_ENDPOINT` in `app/regengine_client.py`.

The three below are RegEngine's **published documentation** paths (its
`frontend/src/app/docs/fsma-204` page). The simulator never calls them,
and at the pinned commit RegEngine's graph service registers those
handlers under different prefixes -- `/api/v1/fsma/traceability/trace/...`
and `/api/v1/fsma/compliance/export/fda-request` -- so confirm against a
running instance before depending on either shape:

- Export endpoint: `GET /v1/fsma/export/fda-request`
- Forward trace: `GET /v1/fsma/trace/forward/{tlc}`
- Backward trace: `GET /v1/fsma/trace/backward/{tlc}`

## Required headers on `POST /api/v1/webhooks/ingest`

The header *names* in this table are pinned against what
`LiveRegEngineClient.ingest` actually sends; the "Required" column and the
notes are prose.

| Header | Required | Source / format |
|---|---|---|
| `Content-Type: application/json` | Yes | Always |
| `X-RegEngine-API-Key` | Yes | Per-tenant API key issued by RegEngine |
| `X-Tenant-ID` | Recommended by RegEngine, always sent by the simulator | RegEngine resolves tenant from body, then API-key lookup, then RBAC principal -- but explicit is safer |
| `Idempotency-Key` | **Required** | UUID per logical request; RegEngine caches 2xx for 24h tenant-scoped |
| `X-Webhook-Signature: sha256=<hex>` | Required when `WEBHOOK_HMAC_SECRET` is set on RegEngine | HMAC-SHA256 over the raw request body bytes |

The simulator sends the signature header only when
`REGENGINE_WEBHOOK_HMAC_SECRET` is set, so the pinned "always sent" set is
the first four.

## Webhook HMAC signing

RegEngine's `_verify_webhook_signature` enforces HMAC-SHA256 when
`WEBHOOK_HMAC_SECRET` is set on the ingest service. It lives in
`services/ingestion/app/webhook_router_v2/security.py` and is applied as a
FastAPI dependency by the ingest route in
`services/ingestion/app/webhook_router_v2/routes.py`.

`webhook_router_v2` is a **package**, not a module: this document
previously cited `services/ingestion/app/webhook_router_v2.py` with line
numbers, a path that no longer exists upstream. Line numbers are not
recorded here any more -- they rot silently, and nothing could check them.

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

RegEngine's `WebhookCTEType`, in `services/ingestion/app/webhook_models.py`:

- `growing` - legacy back-compat, normalizes to farm metadata. Simulator never emits.
- `harvesting`
- `cooling`
- `initial_packing`
- `first_land_based_receiving` - 21 CFR 1.1325, seafood / first-receiver flows.
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
  "location_gln": "0850000010017",
  "timestamp": "2026-04-27T08:30:00Z",
  "kdes": { "...": "..." },
  "input_traceability_lot_codes": null
}
```

- `traceability_lot_code` - `min_length=3`, simulator emits `TLC-YYYYMMDD-NNNNNN`.
- `product_description` - `min_length=1, max_length=500`.
- `quantity` - `gt=0`.
- `unit_of_measure` - RegEngine logs but accepts unknown units; simulator
  uses values in the canonical valid set (`cases`, `lbs`, `kg`, `pallets`, etc.).
- `location_name` and `location_gln` - at least one required; if both absent,
  RegEngine's `require_location` validator looks for location-bearing KDEs.
  The full list it checks is `ship_from_location`, `ship_to_location`,
  `receiving_location`, `ship_from_gln`, `ship_to_gln`.
- `location_gln` - when present it must be a 13-digit GS1 GLN with a valid
  mod-10 check digit. RegEngine's field validator is documented as
  warning-only, but it reads `STRICT_GLN_VALIDATION`, which **defaults to
  true**, and a strict failure raises inside Pydantic -- so a bad check
  digit 422s the whole batch, not just that event. The simulator emits a
  real GLN alongside `location_name` on every engine-generated event (see
  `_gln` in `app/scenarios.py`); an earlier version of this document
  claimed it emitted `location_name` only, which has not been true since
  the location registry landed.
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

## Batch and window limits

Values below are pinned against `app/mock_service.py`, whose constants
exist to mirror these; the "RegEngine source" column is prose.

| Limit | Value | RegEngine source |
|---|---|---|
| `events` per batch | 1-500 | `WebhookPayload.events` `min_length`/`max_length` |
| Event timestamp ceiling | now + 24 h | `IngestEvent.validate_timestamp`, and `WEBHOOK_MAX_EVENT_FUTURE_HOURS` in the route |
| Event timestamp floor | now - 90 days | `WEBHOOK_MAX_EVENT_AGE_DAYS`, checked per event in the route handler |
| Idempotency replay window | 24 h | `IdempotencyMiddleware`, tenant-scoped |

The ceiling is a Pydantic field validator, so tripping it 422s the whole
request. The floor is a route-handler check, so it rejects only the
offending event and the rest of the batch still succeeds.

## Required KDEs per CTE (RegEngine `REQUIRED_KDES_BY_CTE`)

KDE validation is **strict string lookup**. A typo or a split key (e.g.
`reference_document_type` instead of `reference_document`) causes the
event to be rejected with `Missing required KDE '<n>' for <cte> CTE`.

This table is generated-artifact-checked: every row must equal
RegEngine's entry for that CTE with the top-level fields below removed.

| CTE | Required KDEs (beyond top-level fields) |
|---|---|
| `harvesting` | `harvest_date`, `reference_document` |
| `cooling` | `cooling_date`, `reference_document` |
| `initial_packing` | `packing_date`, `reference_document`, `harvester_business_name` |
| `first_land_based_receiving` | `landing_date`, `receiving_location`, `reference_document` |
| `shipping` | `ship_date`, `ship_from_location`, `ship_to_location`, `reference_document`, `tlc_source_reference` |
| `receiving` | `receive_date`, `receiving_location`, `immediate_previous_source`, `reference_document`, `tlc_source_reference` |
| `transformation` | `transformation_date`, `reference_document` |

The CFR paragraph citations this table used to carry were removed. They
are not part of RegEngine's table -- upstream keeps its own citations as
inline comments, and the two disagreed: this document cited 1.1330(b)(6)
for cooling's `reference_document` where RegEngine's comment says
1.1325(b)(7). Nothing here can adjudicate that, so neither number is
asserted. Read them off 21 CFR 1.1325-1.1350 directly.

**Top-level fields RegEngine treats as KDEs during validation:**

`traceability_lot_code`, `product_description`, `quantity`,
`unit_of_measure`, `location_name`, `location_gln`

These come from the typed `IngestEvent` fields, not the `kdes` dict --
RegEngine's `_validate_event_kdes`, in
`services/ingestion/app/webhook_router_v2/validation.py`, merges them into
the lookup ahead of `**event.kdes` -- but the simulator satisfies them as
typed fields too, so the distinction is invisible. `location_gln` is
merged but is not currently required by any CTE row.

## Idempotency

`Idempotency-Key` is required: the ingest route in
`services/ingestion/app/webhook_router_v2/routes.py` depends on
`IdempotencyDependency(strict=True)`. The middleware caches 2xx responses
for 24h, scoped per tenant. The simulator generates a fresh `uuid4().hex`
for each new live delivery request and stores it in each record's
`delivery_metadata`.

Live delivery retries reuse the stored `delivery_metadata.idempotency_key`
when present and group failed records by `(source, idempotency_key)` so
RegEngine can identify the retry and return the cached 2xx response.
Records without prior idempotency metadata fall back to a fresh key.

## Mock export columns expected by this repo

(Used by the simulator's mock RegEngine endpoint for dashboard / FDA
preset rendering -- does NOT affect live ingest.)

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

Generated by `FDA_EXPORT_COLUMNS` in `app/fda_export.py` -- that constant is
the source of truth and this list is pinned equal to it. Two of these
(Ship-To / Previous Source, TLC Source Reference) were added for
FSMA-required Shipping and Receiving KDEs and are **not** in RegEngine's
11-column `export/fda-request` CSV, so the two shapes are currently
diverged. `Event Type (CTE)` was previously mislabelled
`Traceability Lot Code Description`, a column name FSMA 204 does not
define.

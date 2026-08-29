---
name: regengine-api-contract
description: Use this skill when working on RegEngine ingest payloads, FSMA 204 CTE/KDE simulation, lineage tracing, FDA-request export features, or anything that must preserve the current RegEngine webhook contract.
---

# RegEngine API contract skill

## Use this skill when

- the task mentions RegEngine
- the task mentions FSMA 204, CTEs, KDEs, traceability lots, or FDA-request exports
- you are changing ingest payload fields, simulator outputs, or lineage behavior
- you are adding a new delivery target or export path

## Contract reference

Before changing any live-ingest behavior, read
`references/contract.md`. It is this repository's record of the RegEngine
webhook contract -- partly enforced, partly prose, and it says which is
which at the top. `tests/test_contract_document.py` holds the enforced
half to the generated artifact in `tests/data/` and to the code it
describes; the rest was last confirmed by hand at the commit that file
records. It covers:

- live ingest endpoint and required headers
- tenant resolution and `X-Tenant-ID`
- required `Idempotency-Key` behavior
- optional HMAC signing with `X-Webhook-Signature`
- accepted CTE types, including `first_land_based_receiving`
- strict RegEngine KDE requirements by CTE

Do not duplicate the detailed contract shape here. Keeping one detailed
reference avoids stale skill guidance when RegEngine's webhook handler
changes -- and only that one file is under test, so a second copy would be
unchecked by construction.

## Guardrails

- Never break the public ingest shape without updating the mock service, tests,
  README, frontend, and `references/contract.md`.
- If you add new KDEs, keep them additive and preserve existing keys unless the
  RegEngine contract reference explicitly changes.
- When extending the engine, maintain valid lineage between upstream and downstream lots.
- Prefer mock-first development. Live delivery should stay opt-in.

## Checklist before you stop

1. Run `pytest`.
2. Confirm new payload fields serialize cleanly.
3. Confirm the dashboard still renders the latest events.
4. Confirm lineage lookup still works for transformed lots.

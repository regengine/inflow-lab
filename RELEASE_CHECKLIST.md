# Release Checklist

Use this checklist before tagging a demo-ready build or handing the simulator to a design partner.

## Required Verification

- [ ] `pytest`
- [ ] `python3 scripts/smoke_regression.py`
- [ ] `node --check app/static/app.js`
- [ ] `python3 -m compileall app`
- [ ] `git diff --check`

## Contract Checks

- [ ] Mock and live ingest payloads still use top-level `source` plus `events[]`.
- [ ] Each event still includes `cte_type`, `traceability_lot_code`, `product_description`, `quantity`, `unit_of_measure`, `location_name`, `timestamp`, and `kdes`.
- [ ] Mock mode remains the default delivery mode.
- [ ] Live delivery still requires `api_key` and `tenant_id`.
- [ ] New export or dashboard behavior is derived from stored records and does not mutate the ingest contract.

## Operator Flow Checks

- [ ] Dashboard loads without credentials when Basic Auth env vars are unset.
- [ ] Basic Auth returns `401` without valid credentials when env vars are set.
- [ ] Demo fixture loading resets to a known event log.
- [ ] Start, stop, single-step, and reset work from the dashboard.
- [ ] Scenario save/load restores both config and event records.
- [ ] Lot lineage for `TLC-DEMO-FC-OUT-001` includes upstream harvested and packed lots.
- [ ] FDA lot-trace export includes `BATCH-DEMO-FC-001`.
- [ ] EPCIS export includes a `TransformationEvent`.
- [ ] Tenant-scoped requests keep separate event logs and scenario saves.

## Handoff Notes

- [ ] README has the current API surface and setup instructions.
- [ ] `AUTOPILOT_TASKS.md` reflects the current backlog state.
- [ ] No generated data files are staged.
- [ ] Any live endpoint or credential values are excluded from docs, logs, fixtures, saved scenarios, and commits.

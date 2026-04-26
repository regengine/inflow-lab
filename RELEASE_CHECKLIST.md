# Release Checklist

Use this checklist before tagging a demo-ready build or handing the simulator to a design partner.

## Required Verification

- [ ] `pytest`
- [ ] `python3 scripts/smoke_regression.py`
- [ ] `python3 scripts/browser_smoke.py`
- [ ] `node --check app/static/app.js`
- [ ] `python3 -m compileall app scripts`
- [ ] `git diff --check`

## Contract Checks

- [ ] Mock and live ingest payloads still use top-level `source` plus `events[]`.
- [ ] Each event still includes `cte_type`, `traceability_lot_code`, `product_description`, `quantity`, `unit_of_measure`, `location_name`, `timestamp`, and `kdes`.
- [ ] Mock mode remains the default delivery mode.
- [ ] Live delivery still requires `api_key` and `tenant_id`.
- [ ] Live-trial tooling refuses live traffic without `--confirm-live` and mock mode remains the dry-run/default safety path.
- [ ] New export or dashboard behavior is derived from stored records and does not mutate the ingest contract.

## Operator Flow Checks

- [ ] Dashboard loads without credentials when Basic Auth env vars are unset.
- [ ] `/api/healthz` remains available without credentials for container/platform healthchecks.
- [ ] Basic Auth returns `401` without valid credentials when env vars are set.
- [ ] Shared-demo or live-trial deployments set explicit `REGENGINE_CORS_ORIGINS` values instead of wildcard CORS.
- [ ] Dashboard simulator actions do not return `403`; if they do, confirm the browser origin exactly matches `REGENGINE_CORS_ORIGINS`.
- [ ] Shared-demo or live-trial deployments set `REGENGINE_DATA_DIR` to mounted persistent storage.
- [ ] Demo fixture loading resets to a known event log.
- [ ] Start, stop, single-step, and reset work from the dashboard.
- [ ] Scenario save/load restores both config and event records.
- [ ] Lot lineage for `TLC-DEMO-FC-OUT-001` includes upstream harvested and packed lots.
- [ ] FDA lot-trace export includes `BATCH-DEMO-FC-001`.
- [ ] EPCIS export includes a `TransformationEvent`.
- [ ] Tenant-scoped requests keep separate event logs and scenario saves.
- [ ] Protected tenant operations can list, reset, and delete a test tenant when Basic Auth is enabled.
- [ ] For shared-demo releases, `python3 scripts/remote_smoke.py` passes against the deployed HTTPS URL.
- [ ] For shared-demo releases, the manual GitHub **Remote Smoke** workflow passes with repository secrets `REGENGINE_REMOTE_USERNAME` and `REGENGINE_REMOTE_PASSWORD`.
- [ ] For live-trial prep, `python3 scripts/live_trial.py --dry-run-only` passes before any confirmed live batch.

## Handoff Notes

- [ ] README has the current API surface and setup instructions.
- [ ] `DESIGN_PARTNER_DEMO_SCRIPT.md` matches the current fixture names, lot codes, expected exports, and reset flow.
- [ ] `DESIGN_PARTNER_DEMO_SCRIPT.md` remote operator runbook uses env vars and mock delivery for shared-demo commands.
- [ ] `DEPLOYMENT_PROFILES.md` matches the intended local, shared-demo, and live-ingest operating modes.
- [ ] `AUTOPILOT_TASKS.md` reflects the current backlog state.
- [ ] No generated data files are staged.
- [ ] Any live endpoint or credential values are excluded from docs, logs, fixtures, saved scenarios, and commits.

# Codex Autopilot Task Queue

This file is the standing backlog for unattended Codex runs.

## Rules for unattended runs

- Work from top to bottom.
- Only take on one coherent theme per run.
- Only check a box once the repository is still passing `pytest`.
- If a task is too large, complete the safest useful slice and add a note under **Progress notes**.
- If you hit a blocker, do not improvise around the RegEngine payload contract. Record the blocker instead.

## Priority 1

- [x] Add server-sent events so the dashboard updates live without polling.
- [x] Add scenario presets for leafy greens supplier, fresh-cut processor, and retailer readiness demo.
- [x] Add replay mode for previously persisted JSONL events.
- [x] Add CSV bulk import for seed lots or scheduled events.

## Priority 2

- [x] Add clearer lot-lineage views in the dashboard for transformed lots.
- [x] Add richer operator-visible delivery status and retry feedback.
- [x] Add export presets that mimic common FDA-request slices.
- [x] Add deterministic scenario fixtures for demo playback.

## Priority 3

- [x] Add EPCIS 2.0 export scaffolding without breaking the current webhook contract.
- [x] Add per-scenario save/load support.
- [x] Add basic auth and tenant-scoped storage boundaries.

## Priority 4

- [x] Add release smoke regression harness and checklist.
- [x] Add tenant/auth status and EPCIS export controls to the dashboard.
- [ ] Add a design-partner demo script with expected talking points and reset steps.
- [ ] Add deployment profile examples for local, shared demo, and live-ingest modes.

## Progress notes

- Start with the smallest safe change that materially improves demo readiness.
- Favor backend + test changes over flashy UI changes if scope is tight.

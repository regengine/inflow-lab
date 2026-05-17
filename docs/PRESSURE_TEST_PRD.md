# RegEngine Inflow Pressure Testing PRD

## Problem

RegEngine has a one-batch live ingest trial, but that only proves the happy path. We need a controlled way to ramp realistic FSMA 204 webhook traffic from Inflow Lab into a real RegEngine endpoint and observe where ingest starts to slow, reject, or fail.

## Goals

- Prove that RegEngine accepts repeated Inflow Lab batches beyond the single-batch smoke path.
- Ramp volume gradually with clear stop conditions.
- Preserve the current live-safety posture: no live traffic without explicit confirmation and credentials.
- Produce terminal and JSON evidence that can be compared across releases.
- Support parallel pressure without changing the Inflow Lab service by using tenant-scoped worker streams.

## Non-Goals

- This is not an unbounded load generator.
- This does not bypass authentication, HMAC signing, idempotency, or tenant boundaries.
- This does not automatically run against production.
- This does not mutate RegEngine outside the configured live ingest tenant and API key.

## Users

- Engineering operator validating staging or production readiness.
- Founder/operator running a guided design-partner or internal reliability check.
- RegEngine developer investigating ingest regressions.

## Requirements

- The tool must perform a mock dry-run before any live pressure stage.
- The tool must require `--confirm-live` before sending live traffic.
- The tool must parse staged ramps in `<requests>x<batch_size>` format.
- The tool must support multiple tenant-scoped worker streams with `--workers`.
- The tool must stop when configured failure, p95 latency, or error-rate gates are exceeded.
- The tool must report per-stage posted events, failed events, p50 latency, p95 latency, max latency, and events per second.
- The tool must optionally write a JSON report.
- The tool must redact Basic Auth passwords, live API keys, and live tenant ids.

## Operator Workflow

1. Run the mock-only dry run against an Inflow Lab instance.
2. Run a low-volume live stage against staging.
3. Increase stage size and workers only after posted counts and latency look healthy.
4. Save the JSON report with the release or incident notes.
5. Repeat the same plan after relevant RegEngine ingest changes.

## Acceptance Criteria

- Unit tests cover dry-run-only execution, live staged execution, worker tenant fanout, JSON report generation, and abort behavior.
- Focused tests pass locally with the repo virtualenv.
- Documentation shows a conservative live command and a staged pressure command.
- The default pressure plan remains small enough to be safe for a first staging run.

## Initial Ramp Recommendation

Start with:

```bash
python3 scripts/pressure_trial.py --confirm-live --stages 2x1,3x5,3x10 --workers 1 --report-json output/pressure/staging-smoke.json
```

Then increase only one dimension at a time:

```bash
python3 scripts/pressure_trial.py --confirm-live --stages 3x10,5x25,5x50 --workers 2 --max-failures 0 --max-error-rate 0 --timeout-seconds 120 --report-json output/pressure/staging-ramp.json
```

When larger downstream batches are expected to take more than the default live delivery window, set `REGENGINE_LIVE_TIMEOUT_SECONDS` on the Inflow Lab process as well.

After each report, render the operator track:

```bash
python3 scripts/pressure_gravitram.py output/pressure/staging-ramp.json
```

The terminal track preserves the machine-readable JSON while showing batch-level slowdowns and failures in one line per stage.

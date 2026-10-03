# Consolidated QA Report — regengine/inflow-lab @ main

**Date:** 2026-10-02 · **QA lead:** subagent (static, read-only review)
**Scope:** private repo `regengine/inflow-lab`, default branch `main`, accessed via GitHub API only (no clone). ~184 files; full tree pulled, ~50 files fetched selectively (app code, routers, schemas, docs, workflows, deploy config).
**Logs:** `backend-qa.md`, `frontend-qa.md`, `ux-qa.md` in this directory; fetched evidence in `evidence/`.

## Executive summary

Inflow Lab is a mock-first FSMA 204 simulator (FastAPI backend + vanilla-JS operator console) that plays the role of a RegEngine customer's own software. The static review found **no Critical and no High findings**. The codebase is unusually hardened for a demo tool: fail-closed auth for shared deployments, DNS-pinning SSRF egress controls on live delivery, strict tenant-id validation, path-traversal guards on persist paths and tenant deletes, thorough secret redaction in responses/logs/saved configs, `extra="forbid"` on all caller-facing schemas, and CI gates (pytest 3.11+3.12, ruff, mypy, eslint, pip-audit with no ignores, bandit, browser smoke).

Two findings need Christopher's attention: **(1)** the tenant-isolation model is bearer-header-based, not identity-bound — one Basic Auth credential can address any tenant (`MEDIUM`, backend #1); **(2)** the design-partner demo script's secret-rotation command targets the wrong GitHub repo (`MEDIUM`, ux #1). Everything else is Low or cosmetic.

## Findings by severity

### MEDIUM
1. **(Backend) Tenant header takes precedence over authenticated identity.** `evidence/app/auth.py:72`: `requested_tenant = request.headers.get(TENANT_HEADER) or username or config.default_tenant`. With Basic Auth on, any valid credential can read/write any tenant's event log, scenario saves, and integration config by setting `X-RegEngine-Tenant`. Operator routes are auth-gated, but the data plane is not identity-bound. Documented design for a demo tool — needs an explicit accept-or-fix decision.
2. **(UX/Docs) Demo script secret-rotation targets the wrong repo.** `evidence/DESIGN_PARTNER_DEMO_SCRIPT.md:146`: `gh secret set REGENGINE_REMOTE_PASSWORD --repo PetrefiedThunder/regengine_codex_workspace`. The repo is `regengine/inflow-lab`; run as written, the Remote Smoke workflows fail their "secret is required" gate. Fix: change `--repo` to `regengine/inflow-lab`.

### LOW
3. **(Backend) Unbounded CSV import body** — `POST /api/import/csv` accepts unlimited `csv_text`, parsed fully in memory (`evidence/app/csv_importer.py:63-65`). Memory-DoS vector on shared demos; suggest a max-bytes guard.
4. **(Backend) Broad exception handler leaks internals** — `evidence/app/routers/mock_regengine.py:56-60`: non-ValidationError parse failures return `422 detail=str(exc)`.
5. **(Backend) No brute-force protection on Basic Auth** — no rate limiting on the auth path; shared demos rely on it entirely.
6. **(Backend) Floating base image tag** — `evidence/Dockerfile:1` `FROM python:3.12-slim`, not pinned to a digest.
7. **(Frontend) No visible SSE connecting/reconnecting state** — stream failures degrade to stale data silently until the next poll (not fully traced; flag for a live check).
8. **(Frontend) No pending indicator on Refresh-triggered reloads** — panels can look stale during slow responses.
9. **(UX/Docs) Stale `regengine_codex_workspace` references** — README LaunchAgent/systemd examples (`evidence/README.md:659-709`) and `evidence/docs/HMAC_STAGING_VALIDATION.md:3,19`.
10. **(UX/Docs) Remote demo password kept in `/tmp`** — wiped on reboot; runbook has no explicit recovery pointer to the rotation block.

### Verified good (no findings)
- Route inventory matches README API reference exactly (all 8 routers, all methods/paths).
- Input validation: tenant-id regex, persist-path traversal guard, symlink-aware tenant-root deletion guard, `extra="forbid"` schemas, bounded numerics.
- Secret handling: no live secrets in repo; api_key/tenant_id/HMAC secret never returned, logged, or saved (sanitized configs, masked payloads, input-stripped validation-error logs).
- Egress controls: DNS pinning, private/loopback/metadata address refusal, cleartext refusal, per-request endpoint re-resolution.
- Delivery safety: mock default, `scripts/live_trial.py` refuses live traffic without `--confirm-live`, mock dry-run first.
- Tenant-cap DoS guard (50, enforced with/without auth, counts on-disk dirs).
- UI: consistent `escapeHtml` on dynamic HTML, aria labels/live regions, two-click armed destructive reset, password-type API key field, "Configured"/"Not set" only.
- Docs accuracy: fixture names, lot codes, the "13 records" claim, reset body shapes, and the "Sandbox (built-in stand-in)" label all verified against code; no broken internal doc links.

## Files changed

None. **What changed:** n/a (read-only review).

## Tests run

**None — static review only.** No tests were executed, no server was started, no live requests were made. I cannot run code in this task.

## What was NOT tested

- Test execution (pytest suite, browser smoke, contract-pin drift) — not run.
- Live app behavior: first-run welcome, guided tour, SSE stream + polling fallback, and all dashboard flows — documented claims only.
- Secrets-dependent paths: live delivery, HMAC signing, Basic Auth login flow — reviewed statically, never exercised.
- Dependency installation / lockfile integrity (`uv sync --frozen`, `npm ci`) — not run.
- Anything requiring the RegEngine live webhook or a Railway deployment.
- `app/delivery.py`, `app/engine.py`, `app/controller.py` internals beyond the reviewed functions, `styles.css` line-by-line, and `tests/` were not deeply reviewed.

## Ghost-workflow verification (explicit)

Queried `GET /repos/regengine/inflow-lab/actions/workflows` on 2026-10-02: 10 workflows — 6 file-backed (`ci.yml`, `codex-autopilot.yml`, `contract-pin-drift.yml`, `remote-browser-smoke.yml`, `remote-smoke.yml`, `smoke-failure-issue.yml`; all `state: "active"`, all with real names) plus 4 GitHub-managed dynamic entries (Copilot code review, Copilot cloud agent, Dependabot Updates, Dependency Graph). **Confirmed: no workflow with id 370344878, no blank-named workflow, no `state: "deleted"` workflow, no path "BuildFailed".** The main-branch tree contains exactly the 6 workflow files. The orphaned record cannot be matched to any file on main — consistent with the earlier diagnosis that only GitHub Support can clear it.

## Remaining risks

- The unattended `codex-autopilot.yml` workflow has `contents: write` and runs Codex on a weekday cron — any repo-wide work should coordinate with it (per the fleet convention, check/claim before shared-resource work).
- The tenant-isolation caveat (Medium #1) means a leaked shared-demo credential exposes all tenants' demo data; the blast radius is demo data only, by design.
- Static review cannot catch runtime-only issues (race conditions, SSE behavior, browser quirks); the repo's own browser-smoke and remote-smoke harnesses are the right next verification layer.

## Recommended next step

Fix the two Medium findings (one-line doc fix for the `--repo` target; a decision on tenant binding), then run the repo's own gates live: `uv run pytest`, `scripts/smoke_regression.py`, and the browser smoke — none of which this static pass could exercise.

## Decisions needed from Christopher

1. **Tenant isolation model:** is bearer-header tenant selection (`X-RegEngine-Tenant` honored over the authenticated username) acceptable for shared demos, or should a credential be bound to its own tenant? (Backend finding #1.)
2. **Confirm the intended repo for the remote-smoke secret:** `regengine/inflow-lab` (assumed) — then the demo-script `--repo` flag gets corrected. (UX finding #1.)
3. Whether the Low items (CSV body cap, base-image pin, Basic Auth rate limiting) are worth fixing now or accepted as demo-tool trade-offs.

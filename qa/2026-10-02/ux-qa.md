# UX QA — regengine/inflow-lab @ main (static walkthrough, read-only)

**Date:** 2026-10-02 · **Method:** heuristic walkthrough of `README.md`, `DESIGN_PARTNER_DEMO_SCRIPT.md`, `DEPLOYMENT_PROFILES.md`, `RELEASE_CHECKLIST.md`, and `docs/HMAC_STAGING_VALIDATION.md` as a brand-new user, cross-checked against the code. Nothing was executed; every "expected result" below is a docs claim checked against code, not a live observation.

## Walkthrough: brand-new user (local demo)

1. **Prereqs** — README says "Requires Python 3.11+", then `python3 -m pip install --upgrade uv` / `uv sync --group dev` / `uv run uvicorn app.main:app --reload` → open `http://127.0.0.1:8000`. Clear; no missing prerequisite found. Uvicorn's default bind is loopback, consistent with the "keep it on localhost" guidance.
2. **First run** — no auth required by default; welcome dialog + optional 5-step tour (persisted per browser). Console presents as Meridian Fresh Foods. Delivery defaults to mock. The non-production nature is stated up front in README, `REPO_PURPOSE.md`, and a banner in the UI (`index.html:12`).
3. **Golden path** — `python3 run_full_fsma_simulation.py` uses the in-process app and the deterministic fresh-cut fixture; outputs listed. Short and credible.
4. **Demo script happy path** — all fixture names, scenario values, lot codes, record counts (13), and export expectations verified against `app/demo_fixtures.py` and `app/scenarios.py`. Reset curl commands use the correct `/api/simulate/reset` body shape (raw `SimulationConfig`, no `config` wrapper — matches the `extra="forbid"` note in `app/schemas/simulation.py`).
5. **Failure rehearsal** — mock friction toggles (401/402/429) documented in both README and the console; recovery guidance lives in the delivery monitor. Well thought through for onboarding.

## Walkthrough: shared demo operator

`DEPLOYMENT_PROFILES.md` shared-demo profile is coherent: Basic Auth + `REGENGINE_REQUIRE_AUTH=1` in the Docker image (fail-closed verified in `app/main.py`), explicit CORS origins, tenant-scoped storage, mock delivery. The dashboard never sends `X-RegEngine-Tenant` (verified in `app.js`), so browser sessions correctly fall back to the authenticated username as tenant — matching the profile doc's claim. The `/api/operator/*` listing/reset/delete flow is documented and the code matches.

## Findings

### MEDIUM
1. **Demo script sets the remote-smoke secret on the wrong repo** — `DESIGN_PARTNER_DEMO_SCRIPT.md:146`: `gh secret set REGENGINE_REMOTE_PASSWORD --repo PetrefiedThunder/regengine_codex_workspace`. The repo is `regengine/inflow-lab`; run as written, the secret lands on a different (legacy) repo and the GitHub **Remote Smoke** / **Remote Browser Smoke** workflows in this repo fail their "secret is required" gate. The workflow secret names themselves (`REGENGINE_REMOTE_USERNAME`/`REGENGINE_REMOTE_PASSWORD`) are correct and consistent.

### LOW
2. **Stale `regengine_codex_workspace` references** — README LaunchAgent/systemd examples (`README.md:659-709`) and `docs/HMAC_STAGING_VALIDATION.md:3,19` still use the old repo/directory name (`~/Documents/GitHub/regengine_codex_workspace`, `regengine_codex_workspace/.venv`). Cosmetic, but a new contributor following the HMAC doc will `git -C` into a nonexistent directory.
3. **Remote demo password kept in `/tmp`** — the runbook stores the shared-demo Basic Auth password at `/tmp/regengine_inflow_lab_demo_basic_auth_password` (chmod 600). It is wiped on reboot and on redeploy, which the runbook notes ("expected at /tmp/…"), but there is no documented recovery path when it's gone other than re-running the rotation commands — worth one line pointing at the rotation block as the recovery.
4. **Demo script step 1 acknowledges a UI quirk without resolving it** — "The configured tenant shows in the Integrations panel, not the stats cards." A new operator looking at "operator context" sees the tenant in a different panel than the rest of context; minor inconsistency, flagged for awareness.

### Notes (no severity)
- Heuristic check (Nielsen-style): visibility of system status is strong (live region announcements, delivery monitor, per-event delivery states); error prevention is good (mock-first default, `--confirm-live` gating, two-click destructive reset, `extra="forbid"` schemas); the docs are unusually honest about non-goals ("simulated event != production evidence").
- The 15–25 minute demo timing is plausible given the verified deterministic fixture path.
- `RELEASE_CHECKLIST.md` is thorough and its contract checks match the code (top-level `source` + `events[]`, per-event KDE fields, mock default, live gating).

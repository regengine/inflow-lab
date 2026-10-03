# Backend QA — regengine/inflow-lab @ main (static, read-only)

**Date:** 2026-10-02 · **Scope:** private repo `regengine/inflow-lab`, default branch `main`
**Method:** checklist-driven static review + OWASP API-lite. No code executed, no tests run, no live server — findings are based solely on files fetched through the GitHub API (evidence preserved in `evidence/` beside this log).
**Environment note:** the sandbox VM restarted mid-review and `/tmp` was wiped; the fetcher was rebuilt and all evidence re-fetched into the persistent `evidence/` directory. No content was modified.

## What was inspected

| Area | Files |
|---|---|
| App wiring | `app/main.py`, `app/dependencies.py`, `app/exceptions.py`, `app/cors.py`, `app/build_info.py`, `app/runtime_guard.py` |
| AuthN/Z | `app/auth.py`, `app/auth_middleware.py`, `app/tenancy.py` |
| Routers (route inventory vs README API reference) | `app/routers/{events,health,ingestion,integration,mock_regengine,operator,scenarios,simulation}.py` |
| Schemas / input validation | `app/schemas/{domain,health,ingestion,integration,operator,scenarios,simulation,exports}.py` |
| Secret handling | `app/controller.py` (`_sanitize_public_config`, `_sanitize_saved_config`, `integration_status`), `app/store.py` (`mask_secret_in_payload`, `mask_secret_in_string`, `_describe_parse_failure`), `app/regengine_client.py` (HMAC, egress checks) |
| Egress / SSRF controls | `app/regengine_client.py` (DNS pinning, private-address refusal, cleartext refusal), `SECURITY_BOUNDARIES.md` |
| Delivery safety | `scripts/live_trial.py` (`--confirm-live` gating), `app/delivery.py` (not line-reviewed in depth) |
| Dependency hygiene | `pyproject.toml`, `package.json` (+ `uv.lock`, `package-lock.json` presence in tree), `.github/workflows/ci.yml` (pip-audit, bandit, ruff, mypy, eslint gates) |
| Deploy config | `Dockerfile`, `railway.json`, `docker-entrypoint.sh` |
| Docs vs code | `README.md`, `PRODUCTION_SURFACE.md`, `SECURITY_BOUNDARIES.md`, `REPO_PURPOSE.md` |

## Route inventory vs README API reference — PASS

Every route declared in the 8 routers exists in the README "API reference" table with matching method, path, and purpose. Router prefixes: `/api` (events, health, ingestion, scenarios), `/api/simulate`, `/api/integration`, `/api/mock/regengine`, `/api/operator`. No undocumented routes found; no README-documented route missing from code. Notable: `/api/healthz` is deliberately unauthenticated (Docker HEALTHCHECK / railway healthcheck target); `/api/health` passes through tenant/auth resolution.

## Input validation — STRONG

- Tenant ids: strict regex `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` (`app/auth.py:14`); 400 on invalid.
- `persist_path` traversal guard: `_ensure_persist_path_within_root` (`app/tenancy.py`) confines caller-supplied paths to `DATA_ROOT` (absolute) or CWD (relative); tenant-scoped requests ignore the override entirely and get forced paths.
- Tenant-root deletion guard: `assert_within_tenant_root` resolves symlinks before `shutil.rmtree` (`app/tenancy.py`).
- Request bodies use `extra="forbid"` on all caller-facing models (integration, delivery, simulation, ingestion, scenario saves) — misspelled keys 422 instead of silently dropped.
- Bounded numerics: `batch_size` 1–100, `interval_seconds >= 0`, retry `limit` 1–500.
- Live-delivery endpoint: `HttpUrl` + DNS-pinning egress guard (resolves once, dials the validated address, keeps hostname for SNI/verification), refusal of loopback/private/link-local/reserved/multicast/metadata addresses and of cleartext `http://` — `SECURITY_BOUNDARIES.md` "Delivery Boundary" accurately describes all of this.

## AuthN/Z — GOOD, one design-level caveat

- Fail-closed shared deployments: `REGENGINE_REQUIRE_AUTH=1` (set in `Dockerfile`) makes the app refuse to start without Basic Auth creds (`app/main.py` lifespan) — good.
- Middleware rejects untrusted browser origins for unsafe methods when auth is on (`app/auth_middleware.py`); CORS origins are an explicit list, wildcards rejected.
- Operator routes (`/api/operator/*`) require Basic Auth (`require_operator_auth`).
- Secret redaction is thorough: `_sanitize_public_config` (api_key always stripped; tenant_id stripped in live mode), `_sanitize_saved_config` (scenario saves never persist api_key; live delivery downgraded to mock), `IntegrationStatusResponse` exposes booleans only, store-level `mask_secret_in_payload` scrubs `api_key`/`authorization` fields and the raw secret string from logs, and pydantic `ValidationError` messages are stripped of input before logging (`_describe_parse_failure`).
- Tenant-cap DoS mitigation: `REGENGINE_MAX_TENANT_CONTROLLERS` (default 50) enforced regardless of auth state, counted against on-disk dirs too (`app/tenancy.py`).
- No live secrets anywhere in the repo (grep for token/key patterns found only `<redacted>` placeholders in docs).

## Findings

### MEDIUM
1. **Tenant header takes precedence over authenticated identity** — `app/auth.py:72`: `requested_tenant = request.headers.get(TENANT_HEADER) or username or config.default_tenant`. With Basic Auth enabled, any single valid credential can address *any* tenant's controller, event log, and scenario saves by setting `X-RegEngine-Tenant`. Operator management routes are auth-gated, but data-plane tenant isolation is bearer-header-based, not identity-bound. `DEPLOYMENT_PROFILES.md` acknowledges per-partner tenants via distinct headers; for a demo tool this is accepted design, but a shared demo with one credential per partner is not actually tenant-isolated at the auth layer. **Recommendation for Christopher:** decide whether the shared-demo threat model needs per-credential tenant binding, or whether one shared operator credential per deployment is acceptable (document the choice).

### LOW
2. **Unbounded CSV import body** — `POST /api/import/csv` accepts `csv_text` with no size cap (`app/csv_importer.py:parse_csv_import`); parsed fully in memory. Memory-DoS vector on a shared demo. Suggest a max-bytes guard.
3. **Broad `except Exception` leaks internals** — `app/routers/mock_regengine.py:56-60`: non-ValidationError parse failures are returned as `422 detail=str(exc)`, potentially surfacing internal error strings to callers.
4. **No brute-force protection on Basic Auth** — no rate limiting on the auth path; acceptable for a demo tool behind a proxy, but shared demos rely on it entirely.
5. **Floating base image tag** — `Dockerfile:1` `FROM python:3.12-slim` is not pinned to a digest; rebuilds can drift.

### Notes (no severity)
- `IngestPayload.events` has no schema-level max length; the 500-event batch cap is enforced in `mock_service.py` (mirrors the live contract — intentional).
- `scripts/live_trial.py` gating verified: refuses to run without `--confirm-live` or `--dry-run-only`; mock dry-run first — matches README.
- CI gates are solid: pytest (3.11 + 3.12), ruff, mypy, eslint, `node --check`, `uv pip check`, `pip-audit` (no ignores), `bandit -q -r app scripts`, browser smoke.
- The scheduled `codex-autopilot.yml` workflow has `contents: write` and runs Codex unattended on a cron — by design (documented in `AGENTS.md`/`AUTOPILOT_TASKS.md`), but any repo-wide concurrent work should coordinate with it.

## Ghost-workflow verification (explicit confirmation)

Queried `GET /repos/regengine/inflow-lab/actions/workflows` on 2026-10-02. Result: 10 workflows — 6 file-backed (CI, Codex Autopilot, Contract Pin Drift, Remote Browser Smoke, Remote Smoke, Smoke Failure Issue; all `state: "active"`, all with real names and `.github/workflows/*.yml` paths) plus 4 GitHub-managed dynamic entries (Copilot code review, Copilot cloud agent, Dependabot Updates, Dependency Graph). **No workflow with id 370344878 exists in the listing. No workflow has a blank name, `state: "deleted"`, or path "BuildFailed".** The main-branch tree contains exactly the 6 workflow files above. The orphaned record is not reproducible from any file on main — consistent with the earlier diagnosis that it is an orphaned API-side record only GitHub Support can clear.

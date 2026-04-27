# HMAC Staging Validation Runbook

Validate end-to-end HMAC signing between Inflow Lab (`regengine_codex_workspace`) and a RegEngine staging deployment before enabling production enforcement.

Source references used in this runbook:
- Inflow Lab signing path: `app/regengine_client.py:17-119`
- Inflow Lab trial runner: `scripts/live_trial.py:57-67`, `scripts/live_trial.py:216-249`, `scripts/live_trial.py:304-314`
- Inflow Lab stored delivery metadata: `app/models.py:102-116`, `app/main.py:474-479`
- RegEngine ingest response model: `services/ingestion/app/webhook_models.py:253-262`
- RegEngine API key rejection text: `services/ingestion/app/webhook_router_v2.py:106-122`
- RegEngine signature utility log/event names: `services/shared/webhook_security.py:248-259`

## 1. Pre-flight Checklist

Run all checks before touching secrets.

```bash
# Repo 1: Inflow Lab simulator must be on main with PR #43 merged.
git -C ~/Documents/GitHub/regengine_codex_workspace checkout main
git -C ~/Documents/GitHub/regengine_codex_workspace pull
git -C ~/Documents/GitHub/regengine_codex_workspace merge-base --is-ancestor eefde6a HEAD && echo "PR #43 is in history"

# Repo 2: RegEngine should be on main at current HEAD.
git -C ~/Documents/GitHub/RegEngine checkout main
git -C ~/Documents/GitHub/RegEngine rev-parse --short HEAD

# Staging health check (must return 2xx).
export REGEN_STAGING_BASE_URL="https://<staging-url>"
curl -fsS "$REGEN_STAGING_BASE_URL/api/healthz" >/dev/null && echo "staging healthz ok"

# Simulator local test baseline.
cd ~/Documents/GitHub/regengine_codex_workspace
python3 -m pytest
```

Expected pre-flight outcomes:
- `git merge-base --is-ancestor eefde6a HEAD` exits 0 and prints `PR #43 is in history`.
- RegEngine repo is on `main` and at current HEAD.
- `curl` to `/api/healthz` returns 2xx.
- `python3 -m pytest` returns `87 passed`.

Operational prerequisites (manual, no SQL shortcuts):
- Staging tenant exists.
- Staging API key exists for that tenant.
- Access to Railway staging environment is available through normal operator/admin channels.

## 2. Generate And Configure The HMAC Secret

Generate one secret and use it on both sides.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set values in this order:

1. RegEngine staging service (`WEBHOOK_HMAC_SECRET`) via Railway dashboard UI.
2. Simulator shell/session (`REGENGINE_WEBHOOK_HMAC_SECRET`) via direct export.

```bash
# Paste exactly one line from the generator output above.
export REGENGINE_WEBHOOK_HMAC_SECRET='<paste-secret-here>'
```

Byte-identity checks (required):

```bash
# Local simulator side byte count.
printf '%s' "$REGENGINE_WEBHOOK_HMAC_SECRET" | wc -c

# Optional local fingerprint (does not print the secret).
python3 - <<'PY'
import hashlib, os
s = os.environ['REGENGINE_WEBHOOK_HMAC_SECRET'].encode('utf-8')
print('local_len=', len(s))
print('local_sha256=', hashlib.sha256(s).hexdigest())
PY

# RegEngine staging side byte count (Railway one-off command).
railway run --service <regengine-ingestion-service> --environment staging -- \
  python -c "import os;print(len(os.getenv('WEBHOOK_HMAC_SECRET','').encode('utf-8')))"

# Optional staging fingerprint.
railway run --service <regengine-ingestion-service> --environment staging -- \
  python -c "import os,hashlib;s=os.getenv('WEBHOOK_HMAC_SECRET','').encode('utf-8');print(hashlib.sha256(s).hexdigest())"
```

Required interpretation:
- Lengths must match exactly.
- Fingerprints must match exactly.
- Common breakages: trailing newline from clipboard paste, extra whitespace, mismatched shell quoting.

## 3. Validation Run

Configure the trial environment and run exactly one live batch.

```bash
cd ~/Documents/GitHub/regengine_codex_workspace

export REGENGINE_REMOTE_BASE_URL='https://<inflow-lab-url>'
export REGENGINE_REMOTE_USERNAME='<basic-auth-username>'
export REGENGINE_REMOTE_PASSWORD='<basic-auth-password>'
export REGENGINE_REMOTE_TENANT='<inflow-lab-tenant-header>'

export REGENGINE_LIVE_ENDPOINT='https://<regengine-staging-url>/api/v1/webhooks/ingest'
export REGENGINE_LIVE_API_KEY='<regengine-staging-api-key>'
export REGENGINE_LIVE_TENANT_ID='<regengine-staging-tenant-id>'

# Must match WEBHOOK_HMAC_SECRET on RegEngine staging exactly.
export REGENGINE_WEBHOOK_HMAC_SECRET='<same-secret-as-staging>'

python3 scripts/live_trial.py --confirm-live
```

Expected success output (from `scripts/live_trial.py:57-67`):

```text
Live trial completed: base_url=..., demo_tenant=..., mock_posted=1, live_posted=1, live_failed=0, live_delivery_status=posted
```

Expected response/metadata shape (from `app/regengine_client.py:29-33` and `app/regengine_client.py:112-118`):

```json
{
  "response": {
    "accepted": 1,
    "rejected": 0,
    "total": 1,
    "events": [ ... ],
    "ingestion_timestamp": "..."
  },
  "metadata": {
    "delivery_mode": "live",
    "endpoint_host": "...",
    "endpoint_path": "/api/v1/webhooks/ingest",
    "idempotency_key": "...",
    "signed": true,
    "status_code": 200
  }
}
```

`response` keys should align with RegEngine `IngestResponse` (`services/ingestion/app/webhook_models.py:253-262`).

Validate stored event metadata from the simulator API (`app/main.py:474-479`):

```bash
curl -fsS -u "$REGENGINE_REMOTE_USERNAME:$REGENGINE_REMOTE_PASSWORD" \
  -H "X-RegEngine-Tenant: $REGENGINE_REMOTE_TENANT" \
  "$REGENGINE_REMOTE_BASE_URL/api/events?limit=1" | jq '.events[0] | {delivery_status, delivery_metadata, error, delivery_response}'
```

Expected fields:
- `.delivery_status == "posted"`
- `.delivery_metadata.signed == true`
- `.delivery_metadata.status_code == 200`

Validate RegEngine logs for signature verification (no skip/warning path):

```bash
railway logs --service <regengine-ingestion-service> --environment staging | \
  rg "webhook_signature_verified|webhook_signature_invalid|webhook_signature_missing|webhook_signature_unsupported_scheme|webhook_signature_mismatch"
```

Success criterion for logs:
- Verification event present (`webhook_signature_verified`) and no signature-failure lines for the same request.
- If your deployment does not emit positive-case verification logs, absence of `webhook_signature_*` warnings for the same request is acceptable evidence of pass.

## 3.5 Negative Test (Mandatory)

A success-only validation does not prove HMAC enforcement is active -
it only proves a happy-path request was accepted. To confirm RegEngine
is actually checking the signature, run a deliberately-wrong-secret
trial and confirm it is rejected with 401.

Run the negative test in a subshell so your real
`REGENGINE_WEBHOOK_HMAC_SECRET` is not overwritten in your session:

```bash
( export REGENGINE_WEBHOOK_HMAC_SECRET='intentionally-wrong-secret-for-negative-test'
  python3 scripts/live_trial.py --confirm-live; echo "exit_code=$?" )
```

Expected outcome:

- Trial exits non-zero (`exit_code=1` or higher).
- Output includes `live_failed=1` and `live_delivery_status=failed`.
- Inspecting the latest stored event metadata shows the request was
  signed (`delivery_metadata.signed: true`) but rejected by the
  server with a 401 status code.

If the negative test instead reports success, RegEngine is **not**
enforcing signatures. Stop the validation. Verify
`WEBHOOK_HMAC_SECRET` is set on RegEngine staging via the diagnostic
in section 4.2.

After the negative test passes, your shell still has the correct
`REGENGINE_WEBHOOK_HMAC_SECRET` because the wrong value was scoped to
the subshell. Confirm with:

```bash
python3 -c "import os,hashlib;s=os.getenv('REGENGINE_WEBHOOK_HMAC_SECRET','').encode();print(hashlib.sha256(s).hexdigest())"
```

The fingerprint should match what RegEngine staging has set.

## 4. Failure Mode Catalog

Each entry includes simulator symptom, RegEngine symptom, diagnostics, and fix.

### 4.1 Simulator Secret Missing

Simulator-side symptom:
- `python3 scripts/live_trial.py --confirm-live` exits non-zero.
- Summary includes `live_failed=1` and `live_delivery_status=failed`.

RegEngine-side symptom:
- 401 with `missing_webhook_signature` (or equivalent missing-signature detail).

Likely root cause:
- `REGENGINE_WEBHOOK_HMAC_SECRET` was not exported in the simulator shell.

Diagnostics:

```bash
# Simulator secret is empty.
python3 -c "import os; print(bool(os.getenv('REGENGINE_WEBHOOK_HMAC_SECRET')))"

# Latest simulator event metadata shows unsigned request.
curl -fsS -u "$REGENGINE_REMOTE_USERNAME:$REGENGINE_REMOTE_PASSWORD" \
  -H "X-RegEngine-Tenant: $REGENGINE_REMOTE_TENANT" \
  "$REGENGINE_REMOTE_BASE_URL/api/events?limit=1" | jq '.events[0].delivery_metadata'
```

Fix:
- Export `REGENGINE_WEBHOOK_HMAC_SECRET` in the same shell session and rerun.

### 4.2 RegEngine Secret Missing (Silent Partial Pass)

Simulator-side symptom:
- Trial may succeed (`live_failed=0`) even if RegEngine is not enforcing signatures.

RegEngine-side symptom:
- Requests accepted without enforcement.

Likely root cause:
- `WEBHOOK_HMAC_SECRET` unset on RegEngine staging.

Diagnostics:

```bash
railway run --service <regengine-ingestion-service> --environment staging -- \
  python -c "import os; print(bool(os.getenv('WEBHOOK_HMAC_SECRET')))"
```

If this prints `False`, the secret is not set on RegEngine. The negative test in Section 3.5 also catches this case directly.

Fix:
- Set `WEBHOOK_HMAC_SECRET` in Railway dashboard for RegEngine staging.
- Repeat negative probe; it must return 401/failure.

### 4.3 Secrets Mismatch (Byte Drift)

Simulator-side symptom:
- Trial fails (`live_failed=1`, `live_delivery_status=failed`).

RegEngine-side symptom:
- 401 with `invalid_webhook_signature`.

Likely root cause:
- Secrets differ by bytes despite looking similar.

Diagnostics:

```bash
# Simulator side.
printf '%s' "$REGENGINE_WEBHOOK_HMAC_SECRET" | wc -c
python3 -c "import os,hashlib;s=os.getenv('REGENGINE_WEBHOOK_HMAC_SECRET','').encode();print(hashlib.sha256(s).hexdigest())"

# RegEngine staging side.
railway run --service <regengine-ingestion-service> --environment staging -- \
  python -c "import os,hashlib;s=os.getenv('WEBHOOK_HMAC_SECRET','').encode();print(len(s));print(hashlib.sha256(s).hexdigest())"
```

Fix:
- Regenerate one secret and reapply to both sides from one source value.
- Re-check byte count and fingerprint.

### 4.4 Body-Bytes Drift (Secrets Match, Signature Still Fails)

Simulator-side symptom:
- Trial fails with 401 behavior even though secret fingerprints match.

RegEngine-side symptom:
- `invalid_webhook_signature` with matching secret fingerprints.

Likely root cause:
- Request body bytes used for HMAC on simulator do not match bytes verified on RegEngine.

Diagnostics:

```bash
# Confirm secrets are truly identical first.
python3 -c "import os,hashlib;s=os.getenv('REGENGINE_WEBHOOK_HMAC_SECRET','').encode();print(hashlib.sha256(s).hexdigest())"
railway run --service <regengine-ingestion-service> --environment staging -- \
  python -c "import os,hashlib;s=os.getenv('WEBHOOK_HMAC_SECRET','').encode();print(hashlib.sha256(s).hexdigest())"

# Correlate failing request in logs by idempotency key from simulator metadata.
curl -fsS -u "$REGENGINE_REMOTE_USERNAME:$REGENGINE_REMOTE_PASSWORD" \
  -H "X-RegEngine-Tenant: $REGENGINE_REMOTE_TENANT" \
  "$REGENGINE_REMOTE_BASE_URL/api/events?limit=1" | jq -r '.events[0].delivery_metadata.idempotency_key'
railway logs --service <regengine-ingestion-service> --environment staging | rg "<idempotency_key>|webhook_signature_invalid"
```

Fix:
- Escalate as an engineering defect.
- Do not enable production enforcement until raw-byte comparison is instrumented and resolved.

### 4.5 Non-Signature HTTP Rejection

Simulator-side symptom:
- Trial fails, but failure is not signature-specific.

RegEngine-side symptom:
- 401 with `Invalid or missing API key` (`services/ingestion/app/webhook_router_v2.py:118-122`).

Likely root cause:
- Invalid `X-RegEngine-API-Key`, wrong tenant, or unrelated auth/subscription failure.

Diagnostics:

```bash
# Check live trial credentials are set.
python3 - <<'PY'
import os
for k in ("REGENGINE_LIVE_ENDPOINT","REGENGINE_LIVE_API_KEY","REGENGINE_LIVE_TENANT_ID"):
    print(k, bool(os.getenv(k)))
PY

# Inspect latest simulator event error and status code.
curl -fsS -u "$REGENGINE_REMOTE_USERNAME:$REGENGINE_REMOTE_PASSWORD" \
  -H "X-RegEngine-Tenant: $REGENGINE_REMOTE_TENANT" \
  "$REGENGINE_REMOTE_BASE_URL/api/events?limit=1" | jq '.events[0] | {delivery_status, error, delivery_metadata}'
```

Fix:
- Re-issue or correct API key via normal RegEngine admin flow.
- Confirm tenant id is correct and active.

## 5. Sign-Off Criteria

All conditions below must be true before declaring HMAC validation passed:

1. RegEngine staging has `WEBHOOK_HMAC_SECRET` set (verified from Railway environment/runtime).
2. Simulator has `REGENGINE_WEBHOOK_HMAC_SECRET` set to byte-identical value (verified via `wc -c` and fingerprint on both sides).
3. `python3 scripts/live_trial.py --confirm-live` returns success (`live_failed=0`, exit code 0).
4. RegEngine logs show signature verification, not skip-only behavior.
5. Simulator stored event record shows `.delivery_metadata.signed == true` and `.delivery_metadata.status_code == 200`.
6. Negative test passes: deliberately wrong simulator secret causes rejection (non-zero trial, 401 behavior).

Criterion 6 is mandatory. A success-only test does not prove enforcement is active.

## 6. Production Rollout Sequence

After all staging sign-off criteria pass, execute in this order:

1. Set `REGENGINE_WEBHOOK_HMAC_SECRET` on all Inflow Lab deployments first (shared demo and any other live-trial environments).
2. Set `WEBHOOK_HMAC_SECRET` on RegEngine production ingestion service.
3. Within 5 minutes of step 2, run one production validation trial:

```bash
python3 scripts/live_trial.py --confirm-live
```

4. If step 3 fails, immediately unset `WEBHOOK_HMAC_SECRET` on RegEngine production and investigate before re-enabling.

Ordering is mandatory. Enabling RegEngine first while simulator environments are unset can cause broad 401 failures during rollout.

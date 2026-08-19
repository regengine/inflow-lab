#!/usr/bin/env bash
#
# Pre-flight for the "moving the demo to a new service" cutover in
# DEPLOYMENT_PROFILES.md. Answers one question: would repointing the dashboard
# at the new service still serve the same thing?
#
# Read-only by construction. It touches only GET endpoints, because the demo is
# shared and the POST routes the dashboard proxies (simulate start/stop/reset,
# fixture load) mutate its state.
#
# What it CANNOT check, and why it matters more than everything it can:
# whether the new service's REGENGINE_BASIC_AUTH_USERNAME / _PASSWORD match the
# INFLOW_LAB_BASIC_AUTH_USERNAME / _PASSWORD that Vercel injects. Both are
# secrets held by the two platforms, so from outside, a service with *different*
# credentials is indistinguishable from one with the same ones — both answer 401
# to an unauthenticated probe. That is checklist step 2, and it is the failure
# this script exists to remind you is still open.
#
# Usage:
#   scripts/cutover_preflight.sh <old-base-url> <new-base-url>
set -uo pipefail

OLD="${1:-}"
NEW="${2:-}"
if [ -z "$OLD" ] || [ -z "$NEW" ]; then
    echo "usage: $0 <old-base-url> <new-base-url>" >&2
    exit 2
fi

# Every read-only path in the dashboard's proxy contract — keep in sync with
# endpointContract() in RegEngine's frontend/src/app/api/inflow-lab/[...path]/route.ts.
PATHS=(
    "/api/healthz"
    "/api/simulate/status"
    "/api/events"
    "/api/lineage/TLC-DEMO-000001"
    "/api/mock/regengine/export/fda-request"
    "/api/mock/regengine/export/epcis"
    "/api/regengine/export/fda-request"
    "/api/regengine/export/epcis"
)

failures=0

probe() { # url -> "status content-type"
    local out
    out=$(curl -sS --max-time 25 -o /dev/null -w '%{http_code}|%{content_type}' "$1" 2>/dev/null) || out="000|"
    echo "${out%%|*} $(echo "${out#*|}" | cut -d';' -f1)"
}

echo "== Proxy contract: every read-only path must answer alike =="
for path in "${PATHS[@]}"; do
    old_result=$(probe "${OLD}${path}")
    new_result=$(probe "${NEW}${path}")
    if [ "$old_result" = "$new_result" ]; then
        printf '  ok    %-42s %s\n' "$path" "$new_result"
    else
        printf '  DIFF  %-42s old=[%s] new=[%s]\n' "$path" "$old_result" "$new_result"
        failures=$(( failures + 1 ))
    fi
done

echo
echo "== Build identity: the new service must report GitHub-injected metadata =="
# A GitHub-connected service reports RAILWAY_GIT_COMMIT_SHA. REGENGINE_BUILD_SHA
# means a CLI deploy is still overriding it — that variable takes precedence in
# app/build_info.py, so leaving it set makes the new service claim whatever
# commit was last pushed by hand. Delete it when switching to GitHub deploys.
new_build=$(curl -sS --max-time 25 "${NEW}/api/healthz" 2>/dev/null)
new_source=$(printf '%s' "$new_build" | python3 -c "import json,sys;print((json.load(sys.stdin).get('build') or {}).get('commit_source') or '')" 2>/dev/null)
new_sha=$(printf '%s' "$new_build" | python3 -c "import json,sys;print((json.load(sys.stdin).get('build') or {}).get('commit_sha') or '')" 2>/dev/null)
printf '  new service: commit_sha=%s commit_source=%s\n' "${new_sha:-(none)}" "${new_source:-(none)}"
if [ "$new_source" != "RAILWAY_GIT_COMMIT_SHA" ]; then
    echo "  WARN  not GitHub-injected — delete REGENGINE_BUILD_SHA/_BRANCH on the new service"
    failures=$(( failures + 1 ))
fi

echo
echo "== CORS: the new service must trust its own origin (checklist step 1) =="
# A Railway reference variable carries the OLD service's URL, which locks the
# new service out of its own domain. Compared against the old service rather
# than asserted absolutely: matching the thing currently serving production is
# the standard that matters.
for label_url in "old ${OLD}" "new ${NEW}"; do
    label=${label_url%% *}; url=${label_url#* }
    acao=$(curl -sSD - -o /dev/null --max-time 20 -H "Origin: ${url}" "${url}/api/healthz" 2>/dev/null \
        | tr -d '\r' | awk 'tolower($1)=="access-control-allow-origin:"{print $2}')
    printf '  %-3s own-origin allowed: %s\n' "$label" "${acao:-(none)}"
    if [ "$label" = "new" ] && [ "$acao" != "$url" ]; then
        echo "  FAIL  new service does not trust its own origin — set REGENGINE_CORS_ORIGINS to a concrete value"
        failures=$(( failures + 1 ))
    fi
done

echo
if [ "$failures" -gt 0 ]; then
    echo "PRE-FLIGHT FAILED (${failures} problem(s)) — do not cut over yet."
    exit 1
fi
cat <<'DONE'
PRE-FLIGHT PASSED — everything checkable from outside matches.

Still unverified, and the likeliest way this breaks:
  Vercel INFLOW_LAB_BASIC_AUTH_USERNAME / _PASSWORD  must equal
  Railway REGENGINE_BASIC_AUTH_USERNAME / _PASSWORD  on the NEW service,
as concrete values, not reference variables to the old service. If they differ,
every proxied call answers 401 the moment you flip INFLOW_LAB_SERVICE_URL.

After flipping and redeploying Vercel (env is baked at build), confirm with:
  curl -s https://<dashboard-host>/api/inflow-lab/api/simulate/status
A 200 with real payload proves the URL and the credentials both landed. A 401
means the credentials do not match. Do not settle for /api/healthz alone — the
proxy answers 200 with {"offline":true} when the backend is unreachable.
DONE

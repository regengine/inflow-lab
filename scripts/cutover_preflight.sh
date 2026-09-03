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
# The contract loop authenticates. It has to: app/auth_middleware.py exempts only
# OPTIONS and /api/healthz, so every other /api path answers 401 application/json
# to a credential-less probe. Comparing 401 against 401 is not evidence of an
# identical contract -- a new service on a different build, with an empty store,
# or with a broken export path answers 401 the same way. Supply
# REGENGINE_REMOTE_USERNAME / REGENGINE_REMOTE_PASSWORD (the Railway
# REGENGINE_BASIC_AUTH_* values for the OLD service, which must be the same pair
# on both) or the loop refuses to run.
#
# What it CANNOT check, and why those matter more than everything it can. Both
# are invisible from outside by construction, so a green run here is not a clean
# bill of health:
#
#   1. Whether the new service's REGENGINE_BASIC_AUTH_USERNAME / _PASSWORD match
#      the INFLOW_LAB_BASIC_AUTH_USERNAME / _PASSWORD that Vercel injects. This
#      script probes both services with ONE credential pair, so it proves the
#      pair works against both -- not that Vercel holds the same pair. Those are
#      secrets on a different platform. Checklist step 2.
#
#   2. Whether the new service mounts a persistent volume at REGENGINE_DATA_DIR.
#      The demo writes its event history there, and on Railway that path only
#      survives a redeploy if a volume is mounted. A service without one answers
#      200, serves this entire contract correctly, reports the right build
#      identity — and starts from an empty store after every deploy, which for a
#      GitHub-connected service means every push to main. Checklist step 4.
#      Check it in the Railway config, not from out here: the old service's
#      `volumeMounts` mountPath must exist on the new service too.
#
# Usage:
#   REGENGINE_REMOTE_USERNAME=... REGENGINE_REMOTE_PASSWORD=... \
#     scripts/cutover_preflight.sh <old-base-url> <new-base-url>
#
# Set REGENGINE_PREFLIGHT_NO_AUTH=1 only for an instance with Basic Auth
# disabled. A 401 from any probe is a hard failure either way.
set -uo pipefail

OLD="${1:-}"
NEW="${2:-}"
if [ -z "$OLD" ] || [ -z "$NEW" ]; then
    echo "usage: $0 <old-base-url> <new-base-url>" >&2
    exit 2
fi

# Every read-only path in the dashboard's proxy contract that resolves to a
# router mounted in app/main.py. RegEngine's
# frontend/src/app/api/inflow-lab/[...path]/route.ts also allows
# /api/regengine/export/{fda-request,epcis} as a "live-export" boundary, but no
# router in this repo mounts an /api/regengine prefix (the mock exports live
# under /api/mock/regengine), so probing them only ever compared two 404s.
# Reconcile the two repos before adding them back.
PATHS=(
    "/api/healthz"
    "/api/simulate/status"
    "/api/events"
    "/api/lineage/TLC-DEMO-000001"
    "/api/mock/regengine/export/fda-request"
    "/api/mock/regengine/export/epcis"
)

failures=0

CURL_AUTH=()
if [ -n "${REGENGINE_REMOTE_USERNAME:-}" ] && [ -n "${REGENGINE_REMOTE_PASSWORD:-}" ]; then
    CURL_AUTH=(-u "${REGENGINE_REMOTE_USERNAME}:${REGENGINE_REMOTE_PASSWORD}")
elif [ "${REGENGINE_PREFLIGHT_NO_AUTH:-}" != "1" ]; then
    cat >&2 <<'NOAUTH'
REGENGINE_REMOTE_USERNAME / REGENGINE_REMOTE_PASSWORD are not set, so the proxy
contract loop cannot run: with Basic Auth enabled every /api path except
/api/healthz answers 401 application/json on both services, and this script
would print "ok" for all of them and roll up to PRE-FLIGHT PASSED without
comparing anything.

Export the OLD service's REGENGINE_BASIC_AUTH_USERNAME / _PASSWORD and re-run,
or set REGENGINE_PREFLIGHT_NO_AUTH=1 if this instance really has Basic Auth
disabled.
NOAUTH
    exit 2
fi

probe() { # url -> "status content-type body-keys"
    local out status ctype body keys
    out=$(curl -sS --max-time 25 "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" \
        -w '\n%{http_code}|%{content_type}' "$1" 2>/dev/null) || out=$'\n000|'
    status=${out##*$'\n'}
    body=${out%$'\n'*}
    ctype=${status#*|}
    status=${status%%|*}
    # Sorted top-level JSON keys: catches a service answering 200 with a
    # different payload shape, which status + content-type alone cannot.
    keys=$(printf '%s' "$body" | python3 -c \
        "import json,sys;d=json.load(sys.stdin);print(','.join(sorted(d)) if isinstance(d,dict) else type(d).__name__)" \
        2>/dev/null) || keys="-"
    echo "${status} $(echo "${ctype}" | cut -d';' -f1) ${keys:--}"
}

echo "== Proxy contract: every read-only path must answer alike, with credentials =="
for path in "${PATHS[@]}"; do
    old_result=$(probe "${OLD}${path}")
    new_result=$(probe "${NEW}${path}")
    old_status=${old_result%% *}
    new_status=${new_result%% *}

    if [ "$old_status" = "401" ] || [ "$new_status" = "401" ]; then
        printf '  FAIL  %-42s 401 with credentials supplied — old=[%s] new=[%s]\n' \
            "$path" "$old_result" "$new_result"
        failures=$(( failures + 1 ))
        continue
    fi
    if [ "$old_result" != "$new_result" ]; then
        printf '  DIFF  %-42s old=[%s] new=[%s]\n' "$path" "$old_result" "$new_result"
        failures=$(( failures + 1 ))
        continue
    fi
    # Identical failures are not evidence of an identical contract. Every path
    # here is meant to serve content, so a matching 4xx/5xx on both sides means
    # the comparison proved nothing.
    case "$old_status" in
        2*)
            printf '  ok    %-42s %s\n' "$path" "$new_result"
            ;;
        *)
            printf '  FAIL  %-42s both services answer %s — nothing was compared\n' \
                "$path" "$old_status"
            failures=$(( failures + 1 ))
            ;;
    esac
done

echo
echo "== Store contents: the new service must not be serving an empty store =="
# A replacement service with no volume, or one pointed at a fresh
# REGENGINE_DATA_DIR, serves this entire contract correctly from zero events.
event_count() { # url -> integer, or -1 when unreadable
    curl -sS --max-time 25 "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" "${1}/api/simulate/status" 2>/dev/null \
        | python3 -c \
        "import json,sys
try:
    print(int((json.load(sys.stdin).get('stats') or {}).get('total_records', -1)))
except Exception:
    print(-1)" 2>/dev/null || echo -1
}
old_records=$(event_count "$OLD")
new_records=$(event_count "$NEW")
printf '  old total_records=%s   new total_records=%s\n' "$old_records" "$new_records"
if [ "$old_records" -gt 0 ] 2>/dev/null && [ "$new_records" -eq 0 ] 2>/dev/null; then
    echo "  FAIL  the old service has history and the new one has none — check the volume mount"
    failures=$(( failures + 1 ))
fi

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

What this run DID prove: every probed path served 2xx with the same
content-type and the same top-level payload shape on both services, using one
working credential pair, and the new service is not serving an empty store.

Two things remain unverified, both invisible from outside:

  1. Vercel INFLOW_LAB_BASIC_AUTH_USERNAME / _PASSWORD must equal Railway
     REGENGINE_BASIC_AUTH_USERNAME / _PASSWORD on the NEW service, as concrete
     values rather than reference variables to the old one. This script proved
     only that the pair YOU supplied works against both services; it cannot read
     what Vercel holds. If they differ, every proxied call answers 401 the moment
     you flip INFLOW_LAB_SERVICE_URL.

  2. Whether the NEW service's store survives a REDEPLOY. A non-empty store today
     only proves data is there now; on Railway that path persists across
     redeploys solely because a volume is mounted at REGENGINE_DATA_DIR. Check
     the Railway config, not this script.

After flipping and redeploying Vercel (env is baked at build), confirm with:
  curl -s https://<dashboard-host>/api/inflow-lab/api/simulate/status
A 200 with real payload proves the URL and the credentials both landed. A 401
means the credentials do not match. Do not settle for /api/healthz alone — the
proxy answers 200 with {"offline":true} when the backend is unreachable.
DONE

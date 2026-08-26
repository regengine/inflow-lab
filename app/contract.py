"""The RegEngine ingest contract version this simulator implements.

Bump this string in lockstep with RegEngine whenever the wire contract
changes in a way either side must react to: the per-CTE required-KDE
table, required headers (API key / tenant / Idempotency-Key / HMAC),
payload shape, or validation semantics.

The same value lives in RegEngine at
``services/ingestion/app/webhook_models.py`` (``INFLOW_CONTRACT_VERSION``).
Both sides advertise it — inflow-lab via ``/api/healthz`` and
``/api/health``, RegEngine via ``/health`` — so deployed instances can
detect skew instead of failing silently: the console's test-connection
probe reports a ``contract_mismatch`` verdict.

What the cross-repo CI actually asserts
---------------------------------------
Earlier revisions of this docstring said RegEngine's Inflow Lab Contract
CI "asserts equality across the two repos". It does not, as run today.
That job (``.github/workflows/inflow-contract-ci.yml`` in RegEngine)
checks inflow-lab out at a hardcoded ``INFLOW_LAB_REF``. Nothing advances
that SHA and nothing fails when it falls behind, so what it compares
RegEngine against is a pinned old commit of this repo, not ``main`` —
which is already well past it (issue #104). Every wire-facing change
merged here since that SHA has shipped to the shared demo having never
run against a real RegEngine.

The inflow-lab half of the fix is the ``contract-pin`` job in
``.github/workflows/ci.yml``, and it is likewise partial today: without a
``REGENGINE_CONTRACT_REPO_TOKEN`` secret it cannot check RegEngine out, so
it runs only the staleness guard and the documented-wire-shape check and
passes. Configure that secret to turn on the two checks that need the
upstream repo — pin drift and the required-KDE comparison against
RegEngine's own ``REQUIRED_KDES_BY_CTE``. Expect the drift check to be red
the first time it runs; clearing it needs the RegEngine-side change
(advance ``INFLOW_LAB_REF``, or track ``main``) that this repo cannot make.

Version history:
- "1" (2026-07-29): initial pinned contract — 7 CTE types, strict KDE
  lookup per REQUIRED_KDES_BY_CTE, required Idempotency-Key, optional
  HMAC body signing.
- 2026-08-26: no version bump — the wire contract is unchanged — but the
  pin behind it gained a staleness mechanism, because
  ``tests/test_regengine_contract_pin.py`` compares two tables that both
  live in this repo and so can only catch an inflow-lab-side edit (issue
  #140). ``scripts/contract_pin_check.py`` now carries
  ``PIN_CONFIRMED_ON`` — the date a human last read RegEngine's
  ``REQUIRED_KDES_BY_CTE`` — and fails once it is more than
  ``MAX_PIN_AGE_DAYS = 90`` old. Move ``PIN_CONFIRMED_ON`` forward only
  after actually re-reading RegEngine's table and reconciling it with
  ``app/cte_rules.py``; moving the date to silence the failure reinstates
  exactly the silent drift the guard exists to catch. The same script
  bounds cross-repo pin drift (``MAX_PIN_DRIFT_COMMITS``) and checks the
  documented per-event wire shape against what
  ``app.regengine_client.build_wire_body`` emits.
"""

from __future__ import annotations

INFLOW_CONTRACT_VERSION = "1"

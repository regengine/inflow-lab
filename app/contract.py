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
probe reports a ``contract_mismatch`` verdict, and RegEngine's Inflow Lab
Contract CI asserts equality across the two repos.

Version history:
- "1" (2026-07-29): initial pinned contract — 7 CTE types, strict KDE
  lookup per REQUIRED_KDES_BY_CTE, required Idempotency-Key, optional
  HMAC body signing.
"""

from __future__ import annotations

INFLOW_CONTRACT_VERSION = "1"

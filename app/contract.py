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
- Staleness guard added 2026-08-26 (#140): CONTRACT_LAST_CONFIRMED and
  ``assert_contract_pin_is_fresh()`` below fail a pytest test
  (tests/test_client_diagnostics.py) once this pin has gone too long
  without a human re-checking it against RegEngine's real contract.
  ``check_remote_kde_contract()`` is the seam for actually diffing
  REQUIRED_KDES against a machine-readable upstream artifact instead of
  just timing out — unwired by default in this environment; see its
  docstring for exactly what CI wiring would activate it. Neither check
  bumps INFLOW_CONTRACT_VERSION by itself — the wire contract did not
  change; only the tooling that watches it did.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from .cte_rules import REQUIRED_KDES


INFLOW_CONTRACT_VERSION = "1"


# ---------------------------------------------------------------------------
# #140 -- staleness guard for the KDE pin
# ---------------------------------------------------------------------------
#
# Nothing used to notice app.cte_rules.REQUIRED_KDES (pinned against
# RegEngine's real contract by tests/test_regengine_contract_pin.py, and
# against INFLOW_CONTRACT_VERSION above) drifting from RegEngine's actual
# upstream table -- the mock and the pin test could keep silently agreeing
# with each other while both disagreed with production. That is the
# "green demo, failing live post" failure mode the pin exists to catch, and
# nothing enforced it ever got re-checked.
#
# Two independent, separately testable mechanisms close that gap:
#
#  1. assert_contract_pin_is_fresh() -- a dated freshness guard. No network
#     needed, so it runs *today*: tests/test_client_diagnostics.py calls it
#     as an ordinary pytest test, which the existing `pytest` CI job
#     already executes on every push -- no workflow changes required. It
#     forces a human to look at this file and move CONTRACT_LAST_CONFIRMED
#     forward at least every CONTRACT_STALENESS_WINDOW_DAYS, whether or not
#     the contract actually changed. It cannot detect an actual drift by
#     itself; it only guarantees a drift can't go unnoticed forever.
#  2. check_remote_kde_contract() -- a seam for diffing REQUIRED_KDES
#     against a real, machine-readable copy of RegEngine's
#     REQUIRED_KDES_BY_CTE. This is the part #140's proposed fix actually
#     asks for ("a scheduled job that diffs against a shared
#     machine-readable contract artifact"), and this repo/session has no
#     route to RegEngine's source to build that artifact from. Rather than
#     have this half quietly report "still matches" with nothing to check
#     against -- a no-op that is worse than not having the check at all,
#     per #140 -- it raises ContractVerificationUnavailable until a real
#     artifact source is configured. See its docstring for the exact CI
#     wiring this needs and why it is not wired in by default.

CONTRACT_LAST_CONFIRMED = date(2026, 7, 29)
# Last date a human actually compared INFLOW_CONTRACT_VERSION and
# REQUIRED_KDES against RegEngine's real webhook contract -- currently the
# same date "1" was pinned (see Version history above); nothing has been
# re-confirmed since then. Move this forward ONLY after actually
# re-checking against RegEngine's
# services/ingestion/app/webhook_models.py, never merely to silence
# assert_contract_pin_is_fresh().

DEFAULT_CONTRACT_STALENESS_WINDOW_DAYS = 90
# ~1 quarter: long enough that a routine review cadence doesn't nag CI on
# every run, short enough that a pin nobody has looked at for that long is
# genuinely suspect. Override with REGENGINE_CONTRACT_STALENESS_WINDOW_DAYS
# if that cadence turns out to be wrong in practice.
_STALENESS_WINDOW_ENV = "REGENGINE_CONTRACT_STALENESS_WINDOW_DAYS"

# Configure one of these for check_remote_kde_contract() to do anything
# other than fail loudly -- see its docstring.
REMOTE_ARTIFACT_PATH_ENV = "REGENGINE_CONTRACT_ARTIFACT_PATH"
REMOTE_ARTIFACT_URL_ENV = "REGENGINE_CONTRACT_ARTIFACT_URL"


class ContractPinStaleError(RuntimeError):
    """Raised by assert_contract_pin_is_fresh() once CONTRACT_LAST_CONFIRMED
    is older than the staleness window -- see the module docstring."""


class ContractVerificationUnavailable(RuntimeError):
    """Raised by check_remote_kde_contract() when the KDE pin cannot be
    checked against RegEngine's real contract in this environment (no
    artifact source configured, or it could not be loaded/parsed).

    Deliberately a distinct exception from "checked, and it matches": a
    caller that treats this the same as a match would recreate exactly the
    silent no-op #140 exists to rule out.
    """


def contract_staleness_window_days() -> int:
    raw = os.getenv(_STALENESS_WINDOW_ENV, "").strip()
    if not raw:
        return DEFAULT_CONTRACT_STALENESS_WINDOW_DAYS
    try:
        window = int(raw)
    except ValueError:
        return DEFAULT_CONTRACT_STALENESS_WINDOW_DAYS
    return window if window > 0 else DEFAULT_CONTRACT_STALENESS_WINDOW_DAYS


def contract_pin_age_days(today: date | None = None) -> int:
    """Days since CONTRACT_LAST_CONFIRMED, as of *today*.

    Defaults to the current UTC calendar date -- matches the rest of the
    app's ``datetime.now(UTC)`` convention (controller.py, store.py) so
    this doesn't depend on the CI runner's local timezone. Accepts an
    explicit ``today`` so tests can exercise the boundary without waiting
    for the calendar or monkeypatching ``datetime``.
    """
    resolved_today = today if today is not None else datetime.now(UTC).date()
    return (resolved_today - CONTRACT_LAST_CONFIRMED).days


def assert_contract_pin_is_fresh(today: date | None = None) -> None:
    """Fail loudly once nobody has reconfirmed the KDE pin in too long.

    This is the half of #140 that needs no network access, so it can run
    as a plain pytest test (tests/test_client_diagnostics.py) inside the
    existing `pytest` CI job -- no workflow changes required. It cannot
    detect an actual drift by itself (that's check_remote_kde_contract()'s
    job); it only guarantees drift can't silently go unnoticed forever.
    """
    age_days = contract_pin_age_days(today)
    window_days = contract_staleness_window_days()
    if age_days > window_days:
        raise ContractPinStaleError(
            "The RegEngine KDE contract pin (app/contract.py, "
            "app.cte_rules.REQUIRED_KDES, "
            "tests/test_regengine_contract_pin.py) has not been "
            f"reconfirmed in {age_days} days (limit {window_days}, last "
            f"confirmed {CONTRACT_LAST_CONFIRMED.isoformat()}). Re-check "
            "REQUIRED_KDES_BY_CTE in RegEngine's "
            "services/ingestion/app/webhook_models.py; update "
            "INFLOW_CONTRACT_VERSION, REQUIRED_KDES, and the pin test if "
            "anything actually changed; then move CONTRACT_LAST_CONFIRMED "
            "in app/contract.py forward to today."
        )


@dataclass(frozen=True, slots=True)
class RemoteContractDiff:
    """Result of diffing app.cte_rules.REQUIRED_KDES against an upstream
    ``{cte_type: [kde, ...]}`` artifact.

    ``matches`` is the single check most callers need; the rest is
    diagnostic detail for whoever has to fix a mismatch.
    """

    matches: bool
    missing_cte_types: tuple[str, ...]
    unexpected_cte_types: tuple[str, ...]
    mismatched_cte_types: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]


def _local_required_kdes_by_cte() -> dict[str, list[str]]:
    """REQUIRED_KDES re-keyed to plain strings -- CTEType is a ``str``
    Enum, so ``.value`` round-trips through JSON the same way an upstream
    artifact would encode it."""
    return {cte_type.value: list(kdes) for cte_type, kdes in REQUIRED_KDES.items()}


def diff_required_kdes(upstream: Mapping[str, Sequence[str]]) -> RemoteContractDiff:
    """Compare REQUIRED_KDES to *upstream* -- RegEngine's real
    REQUIRED_KDES_BY_CTE, however it was loaded.

    Order-independent: KDE lookup on both sides is strict *set* membership
    per event, not a sequence comparison (see app/cte_rules.py and
    RegEngine's own strict-string-lookup validator), so this compares KDE
    sets per CTE type rather than requiring identical tuple order.
    """
    local = _local_required_kdes_by_cte()
    local_keys = set(local)
    upstream_keys = set(upstream)

    mismatched: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for cte_type in sorted(local_keys & upstream_keys):
        local_kdes = tuple(local[cte_type])
        upstream_kdes = tuple(upstream[cte_type])
        if set(local_kdes) != set(upstream_kdes):
            mismatched[cte_type] = (local_kdes, upstream_kdes)

    return RemoteContractDiff(
        matches=not mismatched and local_keys == upstream_keys,
        missing_cte_types=tuple(sorted(upstream_keys - local_keys)),
        unexpected_cte_types=tuple(sorted(local_keys - upstream_keys)),
        mismatched_cte_types=mismatched,
    )


def _load_artifact_from_path(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_artifact_from_url(url: str) -> Any:
    # Synchronous on purpose: this is a CI/script entry point
    # (`python -m app.contract`), not part of the async request path the
    # rest of this module and regengine_client.py run under.
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    return response.json()


def check_remote_kde_contract() -> RemoteContractDiff:
    """Diff REQUIRED_KDES against a machine-readable copy of RegEngine's
    real REQUIRED_KDES_BY_CTE -- #140's actual proposed fix.

    This session cannot reach RegEngine's repository or any shared
    artifact store, so by default there is nothing here to diff against.
    Rather than assume "no artifact configured" means "still matches" --
    the silent no-op #140 exists to rule out -- this raises
    ContractVerificationUnavailable so a caller (a CI step, a script)
    fails loudly and distinguishably from an actual mismatch.

    Configure exactly one of:
    - REGENGINE_CONTRACT_ARTIFACT_PATH: a local JSON file shaped
      ``{cte_type: [kde, ...]}``, e.g. checked out from wherever RegEngine
      (or a shared artifact repo) publishes REQUIRED_KDES_BY_CTE as a
      prior CI step.
    - REGENGINE_CONTRACT_ARTIFACT_URL: an https:// URL serving the same
      JSON shape (a release asset, an internal artifact store, RegEngine's
      own CI output).

    CI WIRING THIS NEEDS (not implemented here -- .github/workflows/ci.yml
    is outside this file's ownership, and standing up the artifact feed
    itself is outside what this session can reach):

    1. RegEngine's side needs to publish REQUIRED_KDES_BY_CTE as that
       ``{cte_type: [kde, ...]}`` JSON artifact somewhere inflow-lab's CI
       can fetch it -- a release asset on RegEngine's repo, an internal
       artifact bucket, or a small shared contract-artifacts repo checked
       out alongside this one.
    2. A step in .github/workflows/ci.yml sets
       REGENGINE_CONTRACT_ARTIFACT_URL (or checks the artifact out and
       sets REGENGINE_CONTRACT_ARTIFACT_PATH) and runs
       ``uv run python -m app.contract``, failing the build on a non-zero
       exit. Prefer wiring it to the existing weekly
       ``schedule: cron: '0 7 * * 1'`` trigger rather than every PR, since
       it depends on a resource external to this repo and PR-blocking a
       contributor on an artifact-store outage would be worse than the
       weekly cadence catching real drift a few days late.

    Until that artifact exists, wiring this into every-PR CI would just
    turn ContractVerificationUnavailable into a permanent, unactionable
    red build -- worse than the status quo, not better. That is why this
    function is a seam and not a CI step: it is correct and fully testable
    today (see tests/test_client_diagnostics.py, which feeds it a local
    file so the diff logic itself is exercised without any network), but
    activating it for real is an infrastructure decision outside this
    repo.
    """
    path = os.getenv(REMOTE_ARTIFACT_PATH_ENV, "").strip()
    url = os.getenv(REMOTE_ARTIFACT_URL_ENV, "").strip()
    if not path and not url:
        raise ContractVerificationUnavailable(
            "Cannot verify app.cte_rules.REQUIRED_KDES against RegEngine's "
            f"real contract: neither {REMOTE_ARTIFACT_PATH_ENV} nor "
            f"{REMOTE_ARTIFACT_URL_ENV} is set. Expected in an environment "
            "with no access to RegEngine's repository -- see "
            "check_remote_kde_contract's docstring for the CI wiring this "
            "needs. Not treated as a pass: #140 is exactly that a silent "
            "no-op here is worse than no check at all."
        )
    source = path or url
    try:
        artifact = _load_artifact_from_path(path) if path else _load_artifact_from_url(url)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise ContractVerificationUnavailable(
            f"Could not load the upstream KDE contract artifact from "
            f"{source!r}: {exc.__class__.__name__}: {exc}"
        ) from exc
    if not isinstance(artifact, dict):
        raise ContractVerificationUnavailable(
            f"Upstream KDE contract artifact from {source!r} was not a "
            "JSON object of {cte_type: [kde, ...]}."
        )
    return diff_required_kdes(artifact)


def _main() -> int:
    """CI entry point: ``uv run python -m app.contract``.

    Exit codes are distinct so a caller (a workflow step, an operator) can
    tell them apart at a glance: 0 clean; 1 the dated pin is stale; 2 an
    actual mismatch against the upstream artifact; 3 the remote check
    could not run at all (no artifact configured, or it failed to load).
    """
    exit_code = 0
    try:
        assert_contract_pin_is_fresh()
        print(
            f"contract pin confirmed {CONTRACT_LAST_CONFIRMED.isoformat()} "
            f"({contract_pin_age_days()} days ago, "
            f"window {contract_staleness_window_days()} days) -- fresh"
        )
    except ContractPinStaleError as exc:
        print(f"STALE: {exc}", file=sys.stderr)
        exit_code = 1

    try:
        diff = check_remote_kde_contract()
    except ContractVerificationUnavailable as exc:
        print(f"UNVERIFIABLE: {exc}", file=sys.stderr)
        return exit_code or 3

    if not diff.matches:
        print(f"MISMATCH: {diff}", file=sys.stderr)
        return exit_code or 2

    print("REQUIRED_KDES matches the upstream contract artifact")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_main())

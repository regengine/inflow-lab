"""Tests for #138 and #140.

Covers two independent things:
  1. `LiveRegEngineClient.ingest()`'s HTTPStatusError branch (#138) --
     RegEngine's actual response body must reach the raised
     `LiveRegEngineDeliveryError` (message and metadata), masked the same
     way store.py/controller.py already mask secrets, and bounded so a
     pathological body can't be stored/logged verbatim. `check_connection`
     is untouched by #138 and gets one light regression test to confirm
     that.
  2. The contract KDE pin staleness guard in `app/contract.py` (#140) --
     the dated freshness guard (`assert_contract_pin_is_fresh`, no network
     needed) and the machine-readable-artifact diff seam
     (`check_remote_kde_contract`, exercised here entirely against local
     fixtures/tmp files -- this repo has no real network path to
     RegEngine's source, which is the point: it must fail loudly, not
     silently pass, when unconfigured).

     Every freshness assertion here is driven by an injected clock (#211).
     This file used to assert the guard's *wall-clock* verdict, which set
     the whole suite to start failing on 2026-10-28 -- the day the pin as
     written crosses its 90-day window -- with no code change involved and
     nothing to fix but the date. The guard's real-calendar teeth live in
     `app.contract._main()` (`uv run python -m app.contract`, exit code 1),
     whose stale-pin exit code is pinned below, also through an injected
     clock.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

import httpx
import pytest

from app.contract import (
    CONTRACT_LAST_CONFIRMED,
    ContractPinStaleError,
    ContractVerificationUnavailable,
    RemoteContractDiff,
    assert_contract_pin_is_fresh,
    check_remote_kde_contract,
    contract_pin_age_days,
    contract_staleness_window_days,
)
from app.contract import _main as contract_main
from app.cte_rules import REQUIRED_KDES
from app.regengine_client import (
    _MAX_ERROR_BODY_CHARS,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
)
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig
from app.store import MASKED_SECRET


# ---------------------------------------------------------------------------
# #138 -- live ingest failures capture RegEngine's response body
# ---------------------------------------------------------------------------


def make_payload() -> IngestPayload:
    return IngestPayload(
        source="erp",
        events=[
            RegEngineEvent(
                cte_type=CTEType.RECEIVING,
                traceability_lot_code="00012345678901-LOT-2026-001",
                product_description="Romaine Lettuce",
                quantity=500,
                unit_of_measure="cases",
                location_name="Distribution Center #4",
                timestamp=datetime(2026, 2, 5, 8, 30, tzinfo=UTC),
                kdes={
                    "receive_date": "2026-02-05",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                },
            )
        ],
    )


def make_live_config(
    endpoint: str | None = None,
    api_key: str = "test-api-key",
    tenant_id: str = "test-tenant-id",
) -> SimulationConfig:
    return SimulationConfig(
        delivery={
            "mode": "live",
            "endpoint": endpoint,
            "api_key": api_key,
            "tenant_id": tenant_id,
        }
    )


class FailingResponse:
    """Duck-typed stand-in for httpx.Response, carrying a configurable
    error body. Only the members `_extract_error_body`/`raise_for_status`
    actually touch are implemented -- exactly what a real response gives
    `exc.response` inside an `except httpx.HTTPStatusError` block."""

    def __init__(
        self,
        status_code: int,
        *,
        json_body: Any = None,
        text_body: str | None = None,
        has_json_body: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self._has_json_body = has_json_body
        self._text_body = (
            text_body if text_body is not None else (json.dumps(json_body) if has_json_body else "")
        )

    def json(self) -> Any:
        if not self._has_json_body:
            raise ValueError("response body is not JSON")
        return self._json_body

    @property
    def text(self) -> str:
        return self._text_body

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            f"Client error '{self.status_code} Reason' for url 'https://example.test/ingest'",
            request=httpx.Request("POST", "https://example.test/ingest"),
            response=self,  # type: ignore[arg-type]
        )


def make_failing_response(
    status_code: int,
    *,
    json_body: Any = None,
    text_body: str | None = None,
) -> FailingResponse:
    if json_body is not None:
        return FailingResponse(status_code, json_body=json_body, has_json_body=True)
    return FailingResponse(status_code, text_body=text_body or "")


class BrokenBodyResponse:
    """A response whose body can't be read as JSON *or* text -- proves
    _extract_error_body degrades to a placeholder instead of letting a
    secondary exception mask the original HTTPStatusError."""

    status_code = 500

    def json(self) -> Any:
        raise ValueError("not json")

    @property
    def text(self) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated undecodable body")

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            "Server error '500 Internal Server Error' for url 'https://example.test/ingest'",
            request=httpx.Request("POST", "https://example.test/ingest"),
            response=self,  # type: ignore[arg-type]
        )


class FailingAsyncClient:
    """Fake httpx.AsyncClient whose post() always returns a pre-configured
    failing response -- mirrors RecordingAsyncClient in
    tests/test_regengine_client.py, but for the error path."""

    response: Any = None

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "FailingAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(
        self,
        endpoint: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        # extensions: the live client attaches {"sni_hostname": ...} when it
        # pins the validated address for the dial (#207). Accepted and
        # ignored here so this fake keeps working on either path.
        extensions: dict[str, Any] | None = None,
    ) -> Any:
        assert FailingAsyncClient.response is not None
        return FailingAsyncClient.response


def run_failing_ingest(
    monkeypatch: Any,
    response: Any,
    config: SimulationConfig | None = None,
) -> LiveRegEngineDeliveryError:
    FailingAsyncClient.response = response
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", FailingAsyncClient)
    with pytest.raises(LiveRegEngineDeliveryError) as exc_info:
        asyncio.run(LiveRegEngineClient().ingest(make_payload(), config or make_live_config()))
    return exc_info.value


def test_live_ingest_failure_surfaces_regengine_json_detail(monkeypatch: Any) -> None:
    """Acceptance criterion: a live 402 with a JSON detail body appears in
    the raised error, not just the status phrase."""
    body = {
        "detail": "Missing required KDE 'reference_document' for receiving CTE",
        "rejected_events": [{"index": 0, "reason": "missing_kde"}],
    }
    exc = run_failing_ingest(monkeypatch, make_failing_response(402, json_body=body))

    # The status-line text is still present (nothing is thrown away)...
    assert "402" in str(exc)
    # ...but RegEngine's actual reason -- absent before #138 -- is now there too.
    assert "Missing required KDE" in str(exc)
    assert "reference_document" in str(exc)
    assert "reference_document" in exc.metadata["error_body"]
    assert exc.metadata["status_code"] == 402


def test_live_ingest_failure_surfaces_regengine_detail_for_429(monkeypatch: Any) -> None:
    """Same acceptance criterion, exercised for 429 (the other status
    #138's evidence calls out alongside 402)."""
    body = {"detail": "Rate limit exceeded for tenant", "retry_after_seconds": 30}
    exc = run_failing_ingest(monkeypatch, make_failing_response(429, json_body=body))

    assert "Rate limit exceeded" in str(exc)
    assert "Rate limit exceeded" in exc.metadata["error_body"]
    assert exc.metadata["status_code"] == 429


def test_live_ingest_failure_degrades_non_json_body_to_text(monkeypatch: Any) -> None:
    """Acceptance criterion: a non-JSON error body degrades to raw text
    without a secondary exception."""
    exc = run_failing_ingest(
        monkeypatch,
        make_failing_response(502, text_body="<html><body>Bad Gateway</body></html>"),
    )

    assert "Bad Gateway" in str(exc)
    assert "Bad Gateway" in exc.metadata["error_body"]


def test_live_ingest_failure_never_raises_a_secondary_exception_on_unreadable_body(
    monkeypatch: Any,
) -> None:
    """Neither .json() nor .text can be read -- must still surface as
    LiveRegEngineDeliveryError (proven by run_failing_ingest's
    pytest.raises), with a placeholder standing in for the body, rather
    than some other exception escaping from inside the except block."""
    exc = run_failing_ingest(monkeypatch, BrokenBodyResponse())

    assert "error body unavailable" in exc.metadata["error_body"]


def test_live_ingest_failure_masks_configured_api_key_in_json_body(monkeypatch: Any) -> None:
    """Hard constraint: a response body containing the configured API key
    is masked before it is stored or raised."""
    api_key = "sk-live-supersecret-123"
    body = {"detail": f"invalid credentials: key={api_key} rejected"}
    exc = run_failing_ingest(
        monkeypatch,
        make_failing_response(401, json_body=body),
        config=make_live_config(api_key=api_key),
    )

    assert api_key not in str(exc)
    assert api_key not in json.dumps(exc.metadata)
    assert MASKED_SECRET in str(exc)
    assert MASKED_SECRET in exc.metadata["error_body"]


def test_live_ingest_failure_masks_configured_api_key_in_non_json_body(monkeypatch: Any) -> None:
    """Same hard constraint, for the .text fallback path (non-JSON body) --
    mask_secret_in_string, not mask_secret_in_payload, has to do the work."""
    api_key = "sk-live-supersecret-456"
    exc = run_failing_ingest(
        monkeypatch,
        make_failing_response(500, text_body=f"upstream rejected request, bad key {api_key}"),
        config=make_live_config(api_key=api_key),
    )

    assert api_key not in str(exc)
    assert api_key not in json.dumps(exc.metadata)
    assert MASKED_SECRET in exc.metadata["error_body"]


def test_live_ingest_failure_masks_api_key_field_by_name_too(monkeypatch: Any) -> None:
    """Belt and suspenders alongside the by-value tests above:
    mask_secret_in_payload also redacts known secret field names outright
    (the same SECRET_FIELD_NAMES convention _scrub_secrets uses in
    store.py), independent of whether the value matches the configured key."""
    body = {"api_key": "some-value-unrelated-to-configured-key", "detail": "unauthorized"}
    exc = run_failing_ingest(monkeypatch, make_failing_response(401, json_body=body))

    assert "some-value-unrelated-to-configured-key" not in exc.metadata["error_body"]
    assert MASKED_SECRET in exc.metadata["error_body"]


def test_live_ingest_failure_bounds_oversized_non_json_error_body(monkeypatch: Any) -> None:
    """Hard constraint: an oversized body is bounded."""
    huge_text = "x" * 50_000
    exc = run_failing_ingest(monkeypatch, make_failing_response(500, text_body=huge_text))

    error_body = exc.metadata["error_body"]
    assert len(error_body) < 50_000
    assert len(error_body) <= _MAX_ERROR_BODY_CHARS + 100  # cap plus the truncation marker
    assert "truncated" in error_body
    assert len(str(exc)) < 50_000


def test_live_ingest_failure_bounds_oversized_json_error_body(monkeypatch: Any) -> None:
    """Same bound, for a large but well-formed JSON body -- RegEngine
    could conceivably echo back a very long per-event rejection list."""
    huge_body = {"rejected_events": [{"index": i, "reason": "x" * 40} for i in range(2000)]}
    exc = run_failing_ingest(monkeypatch, make_failing_response(422, json_body=huge_body))

    error_body = exc.metadata["error_body"]
    assert len(error_body) <= _MAX_ERROR_BODY_CHARS + 100
    assert "truncated" in error_body


def test_live_ingest_failure_small_body_is_not_truncated(monkeypatch: Any) -> None:
    """A normal, compact RegEngine error is not affected by the bound at
    all -- no truncation marker, full detail intact."""
    body = {"detail": "Missing required KDE 'harvest_date' for harvesting CTE"}
    exc = run_failing_ingest(monkeypatch, make_failing_response(400, json_body=body))

    assert "truncated" not in exc.metadata["error_body"]
    assert "harvest_date" in exc.metadata["error_body"]


class ConnectionProbeAsyncClient:
    """Fake client for check_connection()'s GET probe -- distinct status
    per test, plus a no-op /health response so the contract-version skew
    check inside check_connection doesn't need its own fixture here."""

    probe_status_code: int = 200
    probe_json: dict[str, Any] = {}

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "ConnectionProbeAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: Any = None,
        # extensions: the live client attaches {"sni_hostname": ...} when it
        # pins the validated address for the dial (#207). Accepted and
        # ignored here so this fake keeps working on either path.
        extensions: dict[str, Any] | None = None,
    ) -> Any:
        if url.endswith("/health"):
            return FailingResponse(200, json_body={}, has_json_body=True)
        return FailingResponse(
            ConnectionProbeAsyncClient.probe_status_code,
            json_body=ConnectionProbeAsyncClient.probe_json,
            has_json_body=True,
        )


def test_check_connection_behavior_is_unaffected_by_138(monkeypatch: Any) -> None:
    """Acceptance criterion: check_connection() behavior is unaffected.
    #138 only touches ingest()'s HTTPStatusError branch; check_connection
    never calls raise_for_status() at all, so its status-to-verdict mapping
    (_CONNECTION_VERDICTS) must still work exactly as before."""
    ConnectionProbeAsyncClient.probe_status_code = 402
    ConnectionProbeAsyncClient.probe_json = {"detail": "subscription inactive"}
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", ConnectionProbeAsyncClient)

    result = asyncio.run(LiveRegEngineClient().check_connection(make_live_config()))

    assert result.verdict == "subscription_inactive"
    assert result.status_code == 402


# ---------------------------------------------------------------------------
# #140 -- contract KDE pin staleness guard
# ---------------------------------------------------------------------------


def _clock_at(day: date) -> Callable[[], date]:
    """An injected clock frozen at *day* -- #120's pattern from
    app/mock_service.py (``clock: Callable[[], datetime]``), narrowed to
    the calendar date this guard actually compares."""
    return lambda: day


def test_contract_pin_is_fresh_everywhere_inside_its_window() -> None:
    """The guard must stay quiet for the pin's whole window.

    This replaces a test that called ``assert_contract_pin_is_fresh()``
    with no argument at all, i.e. against the real wall clock (#211). That
    made the entire suite fail on a fixed calendar date -- 2026-10-28 for
    the pin as written -- with no code change involved and nothing to
    "fix" but the date, which is how a check turns into noise people learn
    to skip past. The verdict here is a function of CONTRACT_LAST_CONFIRMED
    and the window only, so it reads the same on any day the suite runs.

    The wall-clock verdict is still enforced, deliberately and separately,
    by ``uv run python -m app.contract`` -- see
    ``test_contract_cli_exits_nonzero_for_a_genuinely_stale_pin`` below.
    """
    window_days = contract_staleness_window_days()
    for offset in (0, 1, window_days // 2, window_days - 1, window_days):
        day = CONTRACT_LAST_CONFIRMED + timedelta(days=offset)
        assert_contract_pin_is_fresh(clock=_clock_at(day))
        assert contract_pin_age_days(clock=_clock_at(day)) == offset


def test_contract_pin_guard_fires_for_a_genuinely_stale_pin() -> None:
    """The other half: the guard still has teeth.

    Defusing the calendar bomb must not mean the guard stopped firing --
    one day past the window it raises, and says which pin and how old.
    """
    window_days = contract_staleness_window_days()
    stale_day = CONTRACT_LAST_CONFIRMED + timedelta(days=window_days + 1)

    with pytest.raises(ContractPinStaleError) as exc_info:
        assert_contract_pin_is_fresh(clock=_clock_at(stale_day))

    assert CONTRACT_LAST_CONFIRMED.isoformat() in str(exc_info.value)
    assert f"{window_days + 1} days" in str(exc_info.value)


def test_contract_pin_verdict_ignores_the_wall_clock(monkeypatch: Any) -> None:
    """Simulate a date past the old bomb date; the verdict must not move.

    ``app.contract._contract_clock`` is the module-level default the guard
    reads when a caller passes no clock. Substituting it here simulates
    running this suite on 2026-12-15 -- well past 2026-10-28, the date the
    previous version of this file was scheduled to start failing on -- and
    both verdicts below still depend only on the date each call is given.
    """
    monkeypatch.setattr("app.contract._contract_clock", _clock_at(date(2026, 12, 15)))
    window_days = contract_staleness_window_days()

    assert_contract_pin_is_fresh(clock=_clock_at(CONTRACT_LAST_CONFIRMED + timedelta(days=window_days)))
    with pytest.raises(ContractPinStaleError):
        assert_contract_pin_is_fresh(
            clock=_clock_at(CONTRACT_LAST_CONFIRMED + timedelta(days=window_days + 1))
        )


def test_contract_cli_exits_nonzero_for_a_genuinely_stale_pin(
    monkeypatch: Any, capsys: Any
) -> None:
    """Where the dated nag actually bites: ``uv run python -m app.contract``.

    Exit code 1 is #140's "the pin is stale" signal, distinct from 3
    ("could not verify against upstream at all", which is what this
    environment always reports for the remote half). Driven through the
    injected clock so it is pinned on any calendar date -- and so the
    guard's real-world teeth are demonstrably still there after #211 moved
    them off the pytest verdict.
    """
    window_days = contract_staleness_window_days()
    monkeypatch.setattr(
        "app.contract._contract_clock",
        _clock_at(CONTRACT_LAST_CONFIRMED + timedelta(days=window_days + 1)),
    )
    monkeypatch.delenv("REGENGINE_CONTRACT_ARTIFACT_PATH", raising=False)
    monkeypatch.delenv("REGENGINE_CONTRACT_ARTIFACT_URL", raising=False)

    assert contract_main() == 1
    assert "STALE" in capsys.readouterr().err

    # Fresh pin, same unconfigured environment: the stale signal is gone
    # and only the unverifiable-remote-half signal (3) remains, so the 1
    # above is really about the pin's age and nothing else.
    monkeypatch.setattr(
        "app.contract._contract_clock",
        _clock_at(CONTRACT_LAST_CONFIRMED + timedelta(days=window_days)),
    )
    assert contract_main() == 3


def test_contract_pin_freshness_against_the_real_calendar() -> None:
    """The dated nag itself, kept visible without arming a time bomb.

    Reports the pin's real age in every run: past the window this raises a
    warning that shows up in pytest's warnings summary (deliberately not
    captured by a ``recwarn``/``pytest.warns`` fixture, which would swallow
    it), rather than failing an unrelated author's PR on a date nobody
    chose. The only
    assertion is one that can never be satisfied by the calendar moving --
    a negative age means CONTRACT_LAST_CONFIRMED was set in the future,
    which is a genuine mistake in the pin, not the passage of time.
    """
    age_days = contract_pin_age_days()
    window_days = contract_staleness_window_days()
    if age_days > window_days:
        warnings.warn(
            "The RegEngine KDE contract pin has not been reconfirmed in "
            f"{age_days} days (limit {window_days}, last confirmed "
            f"{CONTRACT_LAST_CONFIRMED.isoformat()}). Re-check it against "
            "RegEngine and move CONTRACT_LAST_CONFIRMED in app/contract.py "
            "forward; `uv run python -m app.contract` fails while this is "
            "outstanding.",
            stacklevel=1,
        )
    assert age_days >= 0, "CONTRACT_LAST_CONFIRMED is dated in the future"


def test_contract_pin_age_days_computes_from_an_explicit_today() -> None:
    assert contract_pin_age_days(today=CONTRACT_LAST_CONFIRMED) == 0
    assert contract_pin_age_days(today=CONTRACT_LAST_CONFIRMED + timedelta(days=10)) == 10


def test_contract_pin_flags_stale_once_the_window_elapses() -> None:
    window_days = contract_staleness_window_days()
    still_fresh = CONTRACT_LAST_CONFIRMED + timedelta(days=window_days)
    just_stale = CONTRACT_LAST_CONFIRMED + timedelta(days=window_days + 1)

    assert_contract_pin_is_fresh(today=still_fresh)  # exactly at the edge: not stale yet

    with pytest.raises(ContractPinStaleError) as exc_info:
        assert_contract_pin_is_fresh(today=just_stale)
    assert "reconfirmed" in str(exc_info.value)
    assert CONTRACT_LAST_CONFIRMED.isoformat() in str(exc_info.value)


def test_contract_pin_staleness_window_respects_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("REGENGINE_CONTRACT_STALENESS_WINDOW_DAYS", "1")
    assert contract_staleness_window_days() == 1

    with pytest.raises(ContractPinStaleError):
        assert_contract_pin_is_fresh(today=CONTRACT_LAST_CONFIRMED + timedelta(days=2))


def test_contract_pin_staleness_window_ignores_garbage_env_value(monkeypatch: Any) -> None:
    """A malformed override degrades to the documented default rather than
    crashing every test/import that touches the contract module -- same
    defensive convention as _live_timeout_seconds in regengine_client.py."""
    monkeypatch.setenv("REGENGINE_CONTRACT_STALENESS_WINDOW_DAYS", "not-a-number")
    from app.contract import DEFAULT_CONTRACT_STALENESS_WINDOW_DAYS

    assert contract_staleness_window_days() == DEFAULT_CONTRACT_STALENESS_WINDOW_DAYS


def _kde_artifact_matching_local() -> dict[str, list[str]]:
    return {cte_type.value: list(kdes) for cte_type, kdes in REQUIRED_KDES.items()}


def test_remote_kde_contract_check_fails_loudly_when_unconfigured(monkeypatch: Any) -> None:
    """The core #140 requirement: when this repo has no way to verify
    against RegEngine's real contract, it must say so loudly and clearly,
    never quietly report a match. A silent no-op here is worse than no
    check at all."""
    monkeypatch.delenv("REGENGINE_CONTRACT_ARTIFACT_PATH", raising=False)
    monkeypatch.delenv("REGENGINE_CONTRACT_ARTIFACT_URL", raising=False)

    with pytest.raises(ContractVerificationUnavailable) as exc_info:
        check_remote_kde_contract()
    assert "REGENGINE_CONTRACT_ARTIFACT_PATH" in str(exc_info.value)
    assert "REGENGINE_CONTRACT_ARTIFACT_URL" in str(exc_info.value)


def test_remote_kde_contract_check_matches_an_identical_local_artifact(
    monkeypatch: Any, tmp_path: Any
) -> None:
    artifact_path = tmp_path / "kde_contract.json"
    artifact_path.write_text(json.dumps(_kde_artifact_matching_local()), encoding="utf-8")
    monkeypatch.setenv("REGENGINE_CONTRACT_ARTIFACT_PATH", str(artifact_path))

    diff = check_remote_kde_contract()

    assert diff == RemoteContractDiff(
        matches=True,
        missing_cte_types=(),
        unexpected_cte_types=(),
        mismatched_cte_types={},
    )


def test_remote_kde_contract_check_detects_a_real_kde_mismatch(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Simulates RegEngine having dropped a required KDE inflow-lab still
    pins -- exactly the drift #140 exists to catch."""
    upstream = _kde_artifact_matching_local()
    upstream["receiving"] = [kde for kde in upstream["receiving"] if kde != "tlc_source_reference"]
    artifact_path = tmp_path / "kde_contract.json"
    artifact_path.write_text(json.dumps(upstream), encoding="utf-8")
    monkeypatch.setenv("REGENGINE_CONTRACT_ARTIFACT_PATH", str(artifact_path))

    diff = check_remote_kde_contract()

    assert diff.matches is False
    assert set(diff.mismatched_cte_types) == {"receiving"}
    local_kdes, upstream_kdes = diff.mismatched_cte_types["receiving"]
    assert "tlc_source_reference" in local_kdes
    assert "tlc_source_reference" not in upstream_kdes


def test_remote_kde_contract_check_detects_missing_and_unexpected_cte_types(
    monkeypatch: Any, tmp_path: Any
) -> None:
    upstream = _kde_artifact_matching_local()
    del upstream["transformation"]  # our artifact "forgot" a CTE the pin still has
    upstream["new_cte_regengine_added"] = ["some_new_kde"]  # a CTE the pin doesn't know yet

    artifact_path = tmp_path / "kde_contract.json"
    artifact_path.write_text(json.dumps(upstream), encoding="utf-8")
    monkeypatch.setenv("REGENGINE_CONTRACT_ARTIFACT_PATH", str(artifact_path))

    diff = check_remote_kde_contract()

    assert diff.matches is False
    assert diff.missing_cte_types == ("new_cte_regengine_added",)
    assert diff.unexpected_cte_types == ("transformation",)


def test_remote_kde_contract_check_surfaces_unparseable_artifact(
    monkeypatch: Any, tmp_path: Any
) -> None:
    artifact_path = tmp_path / "kde_contract.json"
    artifact_path.write_text("not valid json {{{", encoding="utf-8")
    monkeypatch.setenv("REGENGINE_CONTRACT_ARTIFACT_PATH", str(artifact_path))

    with pytest.raises(ContractVerificationUnavailable) as exc_info:
        check_remote_kde_contract()
    assert str(artifact_path) in str(exc_info.value)


def test_remote_kde_contract_check_surfaces_a_missing_artifact_file(
    monkeypatch: Any, tmp_path: Any
) -> None:
    missing_path = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("REGENGINE_CONTRACT_ARTIFACT_PATH", str(missing_path))

    with pytest.raises(ContractVerificationUnavailable):
        check_remote_kde_contract()


def test_remote_kde_contract_check_rejects_a_non_object_artifact(
    monkeypatch: Any, tmp_path: Any
) -> None:
    artifact_path = tmp_path / "kde_contract.json"
    artifact_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    monkeypatch.setenv("REGENGINE_CONTRACT_ARTIFACT_PATH", str(artifact_path))

    with pytest.raises(ContractVerificationUnavailable):
        check_remote_kde_contract()


def test_remote_kde_contract_check_can_load_from_a_url(monkeypatch: Any) -> None:
    """Exercises the URL-fetch branch entirely offline -- this session has
    no real artifact URL to hit (see check_remote_kde_contract's
    docstring), but the fetch-and-diff logic itself is fully verifiable
    without network access, the same way tests/test_regengine_client.py
    exercises LiveRegEngineClient by faking httpx.AsyncClient."""
    calls: list[str] = []

    class _FakeUrlResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[str]]:
            return _kde_artifact_matching_local()

    def fake_get(url: str, timeout: float | None = None) -> _FakeUrlResponse:
        calls.append(url)
        return _FakeUrlResponse()

    monkeypatch.setattr("app.contract.httpx.get", fake_get)
    monkeypatch.delenv("REGENGINE_CONTRACT_ARTIFACT_PATH", raising=False)
    monkeypatch.setenv(
        "REGENGINE_CONTRACT_ARTIFACT_URL", "https://artifacts.example.test/kde-contract.json"
    )

    diff = check_remote_kde_contract()

    assert diff.matches is True
    assert calls == ["https://artifacts.example.test/kde-contract.json"]

from __future__ import annotations

import asyncio
import json
import urllib.request
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import app.controller as controller_module
from app import jev_followup
from app.main import controller
from app.schemas.domain import CSVImportType, CTEType, DestinationMode, RegEngineEvent
from app.schemas.ingestion import CSVImportRequest
from app.schemas.simulation import DeliveryConfig, SimulationConfig


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class _FakeOpener:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def open(self, request: urllib.request.Request, *, timeout: float) -> _FakeResponse:
        self.calls.append((request, timeout))
        return _FakeResponse(self._payload)


def _answer(choice: object = "proceed", confidence: object = 0.8) -> dict[str, object]:
    return {"type": "choice", "choice": choice, "confidence": confidence}


@pytest.mark.parametrize(
    "answer",
    [
        _answer(choice=[]),
        _answer(choice={}),
        _answer(choice=None),
        _answer(choice="unknown"),
        _answer(confidence=True),
        _answer(confidence=None),
        _answer(confidence=[]),
        _answer(confidence="not-a-number"),
        _answer(confidence=float("nan")),
        _answer(confidence="NaN"),
        _answer(confidence=float("inf")),
        _answer(confidence="Infinity"),
        _answer(confidence=10**400),
        _answer(confidence=-0.1),
        _answer(confidence=1.0001),
    ],
    ids=[
        "list-choice",
        "dict-choice",
        "null-choice",
        "unknown-choice",
        "bool-confidence",
        "null-confidence",
        "container-confidence",
        "nonnumeric-confidence",
        "nan-number",
        "nan-string",
        "infinite-number",
        "infinite-string",
        "overflowing-integer",
        "negative-confidence",
        "over-one-confidence",
    ],
)
def test_recommend_sync_rejects_malformed_remote_answers(
    monkeypatch: pytest.MonkeyPatch,
    answer: dict[str, object],
) -> None:
    monkeypatch.setattr(jev_followup, "_jev_choice", lambda _summary: answer)

    assert jev_followup._recommend_sync({}) is None


@pytest.mark.parametrize("choice", ["proceed", "fix_required_warnings", "regenerate"])
@pytest.mark.parametrize("confidence", [0.6, 0.87543, 1.0, "0.8"])
def test_recommend_sync_preserves_valid_closed_set_answers(
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    confidence: float | str,
) -> None:
    monkeypatch.setattr(jev_followup, "_jev_choice", lambda _summary: _answer(choice, confidence))

    recommendation = jev_followup._recommend_sync({})

    assert recommendation is not None
    assert recommendation["action"] == choice
    assert recommendation["confidence"] == round(float(confidence), 4)
    assert set(recommendation["options"]) == {
        "proceed",
        "fix_required_warnings",
        "regenerate",
    }


def test_recommend_sync_keeps_threshold_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev_followup, "_jev_choice", lambda _summary: _answer(confidence=0.5999))
    assert jev_followup._recommend_sync({}) is None

    monkeypatch.setattr(jev_followup, "_jev_choice", lambda _summary: _answer(confidence=0.6))
    assert jev_followup._recommend_sync({}) is not None


def test_jev_transport_uses_an_opener_that_rejects_every_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"answers": {"followup": _answer()}}
    fake_opener = _FakeOpener(payload)
    installed_handlers: list[object] = []
    legacy_urlopen_calls: list[urllib.request.Request] = []

    def fake_build_opener(*handlers: object) -> _FakeOpener:
        installed_handlers.extend(handlers)
        return fake_opener

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        legacy_urlopen_calls.append(request)
        return _FakeResponse(payload)

    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(jev_followup.urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(jev_followup.urllib.request, "urlopen", fake_urlopen)

    answer = jev_followup._jev_choice({"total_events": 1})

    assert answer == _answer()
    assert legacy_urlopen_calls == []
    assert len(fake_opener.calls) == 1
    redirect_handler = next(
        handler
        for handler in installed_handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    )
    original = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone",
        data=b"{}",
        headers={"Authorization": "Bearer synthetic-test-key"},
        method="POST",
    )
    for code in (301, 302, 303, 307, 308):
        redirected = redirect_handler.redirect_request(
            original,
            None,
            code,
            "redirect",
            {},
            "http://127.0.0.1:9999/credential-sink",
        )
        assert redirected is None


def test_jev_transport_stays_disabled_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        jev_followup.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("transport must not run without a key"),
    )
    monkeypatch.setattr(
        jev_followup.urllib.request,
        "build_opener",
        lambda *_args, **_kwargs: pytest.fail("transport must not run without a key"),
    )

    assert jev_followup._jev_choice({"total_events": 1}) is None


def _harvesting_event(*, kdes: dict[str, Any]) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="TLC-JEV-TEST",
        product_description="Romaine",
        quantity=10,
        unit_of_measure="cases",
        location_name="Test Farm",
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
        kdes=kdes,
    )


def test_batch_completeness_uses_canonical_cte_requirements() -> None:
    missing_cte_kdes = _harvesting_event(kdes={})
    complete = _harvesting_event(
        kdes={
            "harvest_date": "2026-10-10",
            "reference_document": "HARVEST-JEV-TEST",
        }
    )

    assert jev_followup.summarize_batch([missing_cte_kdes])["frac_kde_complete"] == 0.0
    assert jev_followup.summarize_batch([complete])["frac_kde_complete"] == 1.0


def test_batch_completeness_never_rounds_an_incomplete_batch_up_to_complete() -> None:
    complete = _harvesting_event(
        kdes={
            "harvest_date": "2026-10-10",
            "reference_document": "HARVEST-JEV-TEST",
        }
    )
    missing_cte_kdes = _harvesting_event(kdes={})

    summary = jev_followup.summarize_batch([complete] * 20_000 + [missing_cte_kdes])

    assert 0.0 < summary["frac_kde_complete"] < 1.0


def test_batch_summary_preserves_event_like_objects_without_optional_fields() -> None:
    event = SimpleNamespace(
        cte_type=CTEType.HARVESTING,
        traceability_lot_code="TLC-JEV-DUCK",
        product_description="Romaine",
        quantity=10,
        unit_of_measure="cases",
        location_name="Test Farm",
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
        kdes={
            "harvest_date": "2026-10-10",
            "reference_document": "HARVEST-JEV-DUCK",
        },
    )

    summary = jev_followup.summarize_batch([event])

    assert summary["frac_kde_complete"] == 1.0
    assert summary["cte_types"] == ["harvesting"]


def test_import_remains_successful_when_optional_followup_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    async def broken_followup(_events: list[RegEngineEvent]) -> dict[str, Any] | None:
        raise TypeError("synthetic malformed optional response")

    monkeypatch.setattr(controller_module, "recommend_import_followup", broken_followup)
    asyncio.run(
        controller.reset(
            SimulationConfig(
                persist_path=str(tmp_path / "events.jsonl"),
                delivery=DeliveryConfig(mode=DestinationMode.NONE),
            )
        )
    )
    revision_before_import = controller.revision
    request = CSVImportRequest(
        import_type=CSVImportType.SEED_LOTS,
        csv_text=(
            "traceability_lot_code,product_description,quantity,unit_of_measure,location_name\n"
            "TLC-JEV-COMMIT,Romaine,10,cases,Test Farm\n"
        ),
        delivery=DeliveryConfig(mode=DestinationMode.NONE),
    )

    response = asyncio.run(controller.import_csv(request))

    assert response.status == "accepted"
    assert response.accepted == response.stored == 1
    assert response.response is None
    assert [record.event.traceability_lot_code for record in controller.store.all_between()] == [
        "TLC-JEV-COMMIT"
    ]
    assert controller.revision == revision_before_import + 1

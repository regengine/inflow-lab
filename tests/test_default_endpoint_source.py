"""#155 -- the default live RegEngine ingest URL has exactly one home.

The literal ``https://www.regengine.co/api/v1/webhooks/ingest`` used to be
written out independently four times: ``DEFAULT_LIVE_INGEST_ENDPOINT`` in
``app/regengine_client.py``, a JS copy in ``app/static/app.js``, the endpoint
field's placeholder in ``app/static/index.html``, and the deployed-mode usage
example in ``scripts/customer_journey.py``.

The JS copy was the one that actually mattered. ``buildConfig()`` substituted
it whenever the endpoint field was blank and submitted it as an explicit,
non-null endpoint, so the backend's own default was unreachable from the
console: changing the Python constant would have left the operator console
posting live traffic at the stale URL with nothing to notice.

These tests pin the whole chain -- Python constant to API field to console
placeholder to submitted config -- rather than only the grep-level property,
so a regression in any link fails here.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess  # nosec B404 -- runs the local node harness, no shell
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import app, controller
from app.regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT, LiveRegEngineClient
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "tests" / "support" / "console_dom.js"
RESULT_MARKER = "__RESULT__"

NODE = shutil.which("node") or shutil.which("nodejs")

requires_node = pytest.mark.skipif(
    NODE is None, reason="node is required to run app/static/app.js"
)

# A URL that is deliberately NOT the shipped default, used to prove the
# console follows whatever the backend reports instead of a literal of its
# own. Kept off .regengine.co so a stray request could never reach the real
# service, and never dialed by any test here.
SENTINEL_ENDPOINT = "https://relocated.regengine.example/api/v2/webhooks/ingest"


def run_console(snippet: str) -> object:
    """Execute *snippet* inside app.js's own scope and return what it returns.

    Same contract as tests/test_console_behavior.py's helper: app.js runs for
    real under node against the DOM stand-in, which parses the shipped
    index.html, so the markup defaults are the browser's own.
    """
    completed = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [str(NODE), str(HARNESS), str(REPO_ROOT)],
        input=snippet,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"console harness failed (exit {completed.returncode}):\n{completed.stderr}"
        )
    stdout = completed.stdout
    assert RESULT_MARKER in stdout, f"harness produced no result:\n{stdout}\n{completed.stderr}"
    return json.loads(stdout.split(RESULT_MARKER, 1)[1])


@pytest.fixture
def client() -> Any:
    """A TestClient over a controller reset to stock config, both ways.

    app.main.controller is process-wide shared state; resetting on the way out
    as well as on the way in keeps this module order-independent in both
    directions -- it neither inherits another module's live configuration nor
    leaves its own behind.
    """
    asyncio.run(controller.reset(SimulationConfig()))
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        asyncio.run(controller.reset(SimulationConfig()))


# ---------------------------------------------------------------------------
# Criterion 1 -- the literal appears in exactly one Python source location
# ---------------------------------------------------------------------------


def _python_sources() -> list[Path]:
    """Every shipped Python file: the app package, the operator scripts, and
    the repo-root entrypoints. Excludes tests/, whose job is to name the
    concrete URL and assert the app agrees with it."""
    paths = sorted(REPO_ROOT.glob("*.py"))
    paths += sorted((REPO_ROOT / "app").rglob("*.py"))
    paths += sorted((REPO_ROOT / "scripts").rglob("*.py"))
    return paths


def test_the_live_ingest_url_literal_lives_in_exactly_one_python_source() -> None:
    hits = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text().count(
            DEFAULT_LIVE_INGEST_ENDPOINT
        )
        for path in _python_sources()
        if DEFAULT_LIVE_INGEST_ENDPOINT in path.read_text()
    }
    assert hits == {"app/regengine_client.py": 1}, (
        "the default live ingest URL must be written out exactly once, as "
        "DEFAULT_LIVE_INGEST_ENDPOINT in app/regengine_client.py; every other "
        f"reader imports it. Found: {hits}"
    )


def test_the_console_assets_no_longer_carry_their_own_copy() -> None:
    """app.js and index.html must not restate the URL -- both now take it
    from /api/integration/status at runtime."""
    for relative in ("app/static/app.js", "app/static/index.html"):
        source = (REPO_ROOT / relative).read_text()
        assert DEFAULT_LIVE_INGEST_ENDPOINT not in source, (
            f"{relative} hardcodes the live ingest URL again; it must read "
            "default_endpoint from /api/integration/status instead"
        )
    # Naming the Python constant in a comment is how app.js points a reader
    # at the real source; re-DECLARING it is the regression. Only the latter
    # is a second copy, so only the latter is what this rejects.
    app_js = (REPO_ROOT / "app" / "static" / "app.js").read_text()
    declarations = re.findall(
        r"^\s*(?:const|let|var)\s+DEFAULT_LIVE_INGEST_ENDPOINT\b.*$", app_js, re.MULTILINE
    )
    assert declarations == [], (
        "app.js declared its own copy of the constant again -- even pointing "
        f"at the right URL today, it is the thing that drifts tomorrow: {declarations}"
    )


def test_the_egress_allowlist_host_still_matches_the_constant() -> None:
    """``_TRUSTED_REGENGINE_HOST`` in app/schemas/simulation.py is the one
    remaining partial restatement of this URL -- its own comment says it must
    match the host in ``DEFAULT_LIVE_INGEST_ENDPOINT``, and it cannot import
    it because regengine_client imports from that module. It is not the
    literal URL, so it does not violate the single-source rule, but a
    relocated ingest host that left it behind would silently drop the
    allowlist entry and make every live delivery depend on a DNS lookup that
    an offline environment cannot make. Pin the two together instead.
    """
    from app.schemas.simulation import _TRUSTED_REGENGINE_HOST

    assert urlparse(DEFAULT_LIVE_INGEST_ENDPOINT).hostname == _TRUSTED_REGENGINE_HOST


def test_the_customer_journey_help_text_renders_the_shared_constant() -> None:
    """scripts/customer_journey.py documents the deployed-mode env setup in
    its --help output. That example is rendered from the constant now, so it
    still shows a real, copy-pasteable URL without holding a second copy."""
    from scripts import customer_journey as journey

    assert DEFAULT_LIVE_INGEST_ENDPOINT not in (journey.__doc__ or "")
    assert f"REGENGINE_LIVE_ENDPOINT={DEFAULT_LIVE_INGEST_ENDPOINT}" in journey._USAGE


# ---------------------------------------------------------------------------
# Criterion 2 -- the console reads the default from the backend
# ---------------------------------------------------------------------------


def test_integration_status_publishes_the_backend_default(client: Any) -> None:
    body = client.get("/api/integration/status").json()
    assert body["default_endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT
    # With nothing configured the effective endpoint is that same default.
    assert body["endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT


def test_default_endpoint_is_the_default_not_the_effective_endpoint(client: Any) -> None:
    """`endpoint` is where delivery goes right now; `default_endpoint` is what
    a blank field falls back to. They coincide only until something overrides
    the endpoint, and the console's placeholder needs the latter -- showing an
    operator their own configured URL as the "leave blank for this" hint would
    be a lie the moment they cleared the field."""
    response = client.post(
        "/api/integration/configure",
        json={"endpoint": "https://partner.regengine.example/api/v1/webhooks/ingest"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == "https://partner.regengine.example/api/v1/webhooks/ingest"
    assert body["default_endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT


@requires_node
def test_the_console_placeholder_is_filled_from_the_status_response() -> None:
    """The shipped markup carries no placeholder at all; the one an operator
    sees is whatever the backend just reported."""
    result = run_console(
        f"""
        __dom.routes({{
          '/api/integration/status': {{
            mode: 'mock',
            endpoint: {json.dumps(SENTINEL_ENDPOINT)},
            endpoint_host: 'relocated.regengine.example',
            default_endpoint: {json.dumps(SENTINEL_ENDPOINT)},
            api_key_configured: false,
            tenant_configured: false,
            hmac_configured: false,
            contract_version: '1.0.0',
            mock_friction: [],
          }},
        }});
        const fromMarkup = ids.endpoint.getAttribute('placeholder');
        await loadIntegrationStatus();
        return {{ fromMarkup, afterLoad: ids.endpoint.getAttribute('placeholder') }};
        """
    )
    assert result["fromMarkup"] in (None, ""), (
        "index.html carries a hardcoded placeholder again -- it is the third "
        "copy of the URL #155 removed"
    )
    assert result["afterLoad"] == SENTINEL_ENDPOINT


# ---------------------------------------------------------------------------
# The bug itself -- a blank field must not be filled in by the console
# ---------------------------------------------------------------------------


@requires_node
def test_a_blank_endpoint_field_is_submitted_as_null() -> None:
    """buildConfig() used to substitute the JS literal here, which is what made
    the backend's default unreachable. A typed endpoint still wins."""
    result = run_console(
        """
        ids.endpoint.value = '';
        const blank = buildConfig().delivery.endpoint;
        ids.endpoint.value = '  https://typed.regengine.example/api/v1/webhooks/ingest  ';
        const typed = buildConfig().delivery.endpoint;
        return { blank, typed };
        """
    )
    assert result["blank"] is None
    assert result["typed"] == "https://typed.regengine.example/api/v1/webhooks/ingest"


def test_the_backend_accepts_the_consoles_blank_field_body(client: Any) -> None:
    """The delivery schema takes a null endpoint (HttpUrl | None), so sending
    null is a smaller change than teaching the console to substitute -- and it
    is the only form that actually defers to the server."""
    response = client.post(
        "/api/simulate/reset",
        json={
            "source": "codex-simulator",
            "scenario": "leafy_greens_supplier",
            "scale": "midsize",
            "interval_seconds": 1.5,
            "batch_size": 3,
            "seed": None,
            "persist_path": "data/events.jsonl",
            "delivery": {
                "mode": "mock",
                "endpoint": None,
                "api_key": None,
                "tenant_id": None,
                "mock_friction": [],
            },
        },
    )
    assert response.status_code == 200, response.text

    stored = client.get("/api/simulate/status").json()["config"]["delivery"]
    assert stored["endpoint"] is None, "a null endpoint must stay null, not be materialized"
    status = client.get("/api/integration/status").json()
    assert status["endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT


# ---------------------------------------------------------------------------
# Criterion 3 -- changing the Python constant changes console behavior with
# no JS edit
# ---------------------------------------------------------------------------


def test_moving_the_python_constant_moves_the_api_answer(monkeypatch: Any, client: Any) -> None:
    """controller.integration_status() reads the constant by name, so a
    relocated ingest URL reaches the API with no other edit."""
    monkeypatch.setattr("app.controller.DEFAULT_LIVE_INGEST_ENDPOINT", SENTINEL_ENDPOINT)

    body = client.get("/api/integration/status").json()

    assert body["default_endpoint"] == SENTINEL_ENDPOINT
    assert body["endpoint"] == SENTINEL_ENDPOINT
    assert body["endpoint_host"] == "relocated.regengine.example"


def test_moving_the_python_constant_moves_where_a_null_endpoint_is_delivered(
    monkeypatch: Any,
) -> None:
    """The other half: a config carrying the console's null endpoint is posted
    to whatever the constant now says, so console traffic follows it too."""
    posted: list[str] = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"accepted": 1}

    class _RecordingAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "_RecordingAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(self, endpoint: str, **kwargs: Any) -> _FakeResponse:
            posted.append(endpoint)
            return _FakeResponse()

    monkeypatch.setattr("app.regengine_client.DEFAULT_LIVE_INGEST_ENDPOINT", SENTINEL_ENDPOINT)
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", _RecordingAsyncClient)

    payload = IngestPayload(
        source="codex-simulator",
        events=[
            RegEngineEvent(
                cte_type=CTEType.RECEIVING,
                traceability_lot_code="00012345678901-LOT-2026-155",
                product_description="Romaine Lettuce",
                quantity=12,
                unit_of_measure="cases",
                location_name="Distribution Center #4",
                timestamp="2026-03-01T08:00:00Z",
                kdes={
                    "receive_date": "2026-03-01",
                    "receiving_location": "Distribution Center #4",
                    "ship_from_location": "Valley Fresh Farms",
                },
            )
        ],
    )
    config = SimulationConfig(
        delivery={
            "mode": "live",
            "endpoint": None,  # exactly what buildConfig() sends for a blank field
            "api_key": "test-api-key",
            "tenant_id": "test-tenant-id",
        }
    )

    asyncio.run(LiveRegEngineClient().ingest(payload, config))

    assert posted == [SENTINEL_ENDPOINT]


@requires_node
def test_the_console_default_follows_the_python_constant_with_no_js_edit(
    monkeypatch: Any,
) -> None:
    """End-to-end proof of the acceptance criterion.

    The constant is moved in Python only; the *real* /api/integration/status
    body that produces is replayed to the console under node, and the byte
    content of app.js is checked to be untouched on both sides of the move.
    What the operator would see -- and what a blank field falls back to --
    tracks the constant with no JavaScript changed.
    """
    app_js = REPO_ROOT / "app" / "static" / "app.js"
    before = app_js.read_bytes()

    monkeypatch.setattr("app.controller.DEFAULT_LIVE_INGEST_ENDPOINT", SENTINEL_ENDPOINT)
    asyncio.run(controller.reset(SimulationConfig()))
    try:
        with TestClient(app) as test_client:
            status_body = test_client.get("/api/integration/status").json()
    finally:
        asyncio.run(controller.reset(SimulationConfig()))

    assert status_body["default_endpoint"] == SENTINEL_ENDPOINT

    result = run_console(
        f"""
        __dom.routes({{ '/api/integration/status': {json.dumps(status_body)} }});
        await loadIntegrationStatus();
        ids.endpoint.value = '';
        return {{
          placeholder: ids.endpoint.getAttribute('placeholder'),
          submittedEndpoint: buildConfig().delivery.endpoint,
        }};
        """
    )

    assert result["placeholder"] == SENTINEL_ENDPOINT
    # Still null, not the relocated URL: the console never substitutes a
    # default of its own, it defers to the server's.
    assert result["submittedEndpoint"] is None
    assert app_js.read_bytes() == before, "app.js must not need editing for this to hold"

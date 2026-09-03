"""Hold `contract.md` to the claims it makes (#211).

``.agents/skills/regengine-api-contract/references/contract.md`` is the
reference the skill tells every agent to read before touching live-ingest
behavior. It used to open by calling itself the "source of truth" and a
"mirror of RegEngine's contract" while nothing checked either claim -- and
by the time #211 was filed it had drifted: it cited a RegEngine module
path that had become a package, said events older than 90 days were
"accepted but flagged" when RegEngine rejects them, said the simulator
emitted ``location_name`` only when it emits a GLN on every engine event,
and carried a sample GLN whose GS1 check digit was wrong.

#104 built the machinery that makes part of the document checkable:
``scripts/regengine_kde_contract.py`` extracts RegEngine's real
``REQUIRED_KDES_BY_CTE`` from its source, and
``tests/data/regengine_required_kdes.json`` is the generated artifact,
checksummed and provenance-pinned by ``tests/test_contract_provenance.py``.
This file spends that machinery on the document itself.

The split is deliberate and matches what the document now claims about
itself:

* every test above ``REGENGINE_CHECKOUT`` runs offline on every ``pytest``
  invocation, and compares the document to generated data or to the code
  it describes -- never to a second copy of the same prose;
* the tests below it read a real RegEngine checkout and are the only
  things that can catch a wrong upstream path or symbol. They skip unless
  ``REGENGINE_CHECKOUT`` is set, exactly like
  ``test_snapshot_matches_a_real_regengine_checkout`` in
  ``tests/test_contract_provenance.py``.

Claims the document makes that neither half can check -- header semantics,
RegEngine's tenant-resolution order, the export/trace endpoint shapes, CFR
citations -- are marked as unverified prose in the document rather than
asserted here. A test that pretends to verify prose is worse than no test.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from app.engine import gs1_check_digit
from app.fda_export import FDA_EXPORT_COLUMNS
from app.mock_service import (
    IDEMPOTENCY_TTL_HOURS,
    LOCATION_KDE_FIELDS,
    MAX_BATCH_EVENTS,
    MAX_EVENT_AGE_DAYS,
    MAX_FUTURE_HOURS,
    MIN_BATCH_EVENTS,
)
from app.regengine_client import (
    DEFAULT_LIVE_INGEST_ENDPOINT,
    WEBHOOK_HMAC_SECRET_ENV,
    LiveRegEngineClient,
)
from app.schemas.domain import CTEType, RegEngineEvent
from app.schemas.ingestion import IngestPayload
from app.schemas.simulation import SimulationConfig
from scripts.regengine_kde_contract import REGENGINE_SOURCE_PATH, load_snapshot


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DOC = REPO_ROOT / ".agents" / "skills" / "regengine-api-contract" / "references" / "contract.md"

# Same environment variable tests/test_contract_provenance.py uses, so one
# `REGENGINE_CHECKOUT=... pytest tests/` run exercises both files' cross-repo
# halves.
REGENGINE_CHECKOUT_ENV = "REGENGINE_CHECKOUT"

# RegEngine paths this document names, and the symbols it attributes to
# each. Written out here rather than scraped from the prose on purpose: the
# document also *mentions* the stale `webhook_router_v2.py` path in the
# sentence explaining why it is gone, and a scraper would demand that a
# deliberately-dead path still exist.
DOCUMENTED_UPSTREAM_SYMBOLS: dict[str, tuple[str, ...]] = {
    "services/ingestion/app/webhook_models.py": (
        "WebhookCTEType",
        "REQUIRED_KDES_BY_CTE",
        "IngestEvent",
        "WebhookPayload",
        "require_location",
        "STRICT_GLN_VALIDATION",
    ),
    "services/ingestion/app/webhook_router_v2/security.py": (
        "_verify_webhook_signature",
        "_validate_event_timestamp_window",
        "WEBHOOK_HMAC_SECRET",
        "WEBHOOK_MAX_EVENT_AGE_DAYS",
        "WEBHOOK_MAX_EVENT_FUTURE_HOURS",
    ),
    "services/ingestion/app/webhook_router_v2/routes.py": (
        "IdempotencyDependency(strict=True)",
        "X-RegEngine-API-Key",
        "X-Tenant-ID",
    ),
    "services/ingestion/app/webhook_router_v2/validation.py": ("_validate_event_kdes",),
}

# The one upstream path the document names on purpose while asserting it is
# GONE -- the pre-split `webhook_router_v2` module. Everything else it names
# has to be a path the checkout tests above actually verify.
QUARANTINED_UPSTREAM_PATHS = frozenset({"services/ingestion/app/webhook_router_v2.py"})


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def _document() -> str:
    return CONTRACT_DOC.read_text(encoding="utf-8")


def _section(title_prefix: str) -> str:
    """The body of the ``## <title_prefix>...`` section, up to the next ``##``.

    Raises rather than returning "" for a missing heading: a renamed or
    deleted section must fail loudly here, not silently make every
    assertion about it vacuous.
    """
    body: list[str] = []
    collecting = False
    for line in _document().splitlines():
        if line.startswith("## "):
            if collecting:
                break
            collecting = line[3:].startswith(title_prefix)
            continue
        if collecting:
            body.append(line)
    if not collecting:
        raise AssertionError(f"contract.md has no `## {title_prefix}...` section")
    return "\n".join(body)


def _backticked(text: str) -> list[str]:
    """Names in single backticks, with fenced blocks removed first.

    A ```` ``` ```` fence would otherwise let the single-backtick pattern
    match across it and swallow whole paragraphs as one "name".
    """
    return re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", text, flags=re.DOTALL))


def _table_rows(section: str) -> list[list[str]]:
    """Data rows of the first markdown table in *section*, cells stripped."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):  # separator row
            continue
        rows.append(cells)
    return rows[1:] if rows else []  # drop the header row


def _bullets(section: str) -> list[str]:
    return [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]


def _documented_kde_table() -> dict[str, set[str]]:
    table: dict[str, set[str]] = {}
    for cells in _table_rows(_section("Required KDEs per CTE")):
        cte = _backticked(cells[0])
        assert len(cte) == 1, f"KDE table row does not name exactly one CTE: {cells[0]!r}"
        table[cte[0]] = set(_backticked(cells[1]))
    return table


def _documented_top_level_fields() -> set[str]:
    marker = "**Top-level fields RegEngine treats as KDEs during validation:**"
    section = _section("Required KDEs per CTE")
    assert marker in section, "contract.md no longer names its top-level-field list"
    after = section.split(marker, 1)[1]

    block: list[str] = []
    for line in after.splitlines():
        if line.strip():
            block.append(line)
        elif block:
            break
    return set(_backticked(" ".join(block)))


def _documented_limits() -> dict[str, list[int]]:
    limits: dict[str, list[int]] = {}
    for cells in _table_rows(_section("Batch and window limits")):
        label = cells[0].replace("`", "").strip()
        limits[label] = [int(value) for value in re.findall(r"\d+", cells[1])]
    return limits


def _documented_sample_payload() -> dict[str, Any]:
    match = re.search(r"```json\n(.*?)```", _section("Per-event payload fields"), re.DOTALL)
    assert match, "contract.md no longer carries a JSON sample payload"
    return json.loads(match.group(1))


def _documented_header_names() -> list[str]:
    names: list[str] = []
    for cells in _table_rows(_section("Required headers on")):
        header = _backticked(cells[0])
        assert header, f"header table row names no header: {cells[0]!r}"
        names.append(header[0].split(":", 1)[0].strip())
    return names


# ---------------------------------------------------------------------------
# The required-KDE table, against the artifact generated from RegEngine
# ---------------------------------------------------------------------------


def test_documented_kde_table_covers_exactly_the_artifacts_cte_types() -> None:
    assert set(_documented_kde_table()) == set(load_snapshot().table)


def test_documented_kde_rows_equal_the_artifact_minus_the_top_level_fields() -> None:
    """The document prints each CTE's requirements "beyond top-level
    fields", so the row it prints must be RegEngine's real row with exactly
    those fields removed -- no quietly dropped KDE, no invented one.

    Both halves of the comparison come from outside the prose: the
    requirements from the generated artifact, and the subtraction set from
    the document's own top-level list, which the next test pins against the
    typed event model. Deleting a KDE from a row, adding one, or hiding one
    by smuggling it into the top-level list all fail here.
    """
    artifact = load_snapshot().table
    documented = _documented_kde_table()
    top_level = _documented_top_level_fields()

    for cte_type, upstream_kdes in sorted(artifact.items()):
        expected = set(upstream_kdes) - top_level
        assert documented[cte_type] == expected, (
            f"contract.md's {cte_type} row is {sorted(documented[cte_type])}; RegEngine's "
            f"table minus the documented top-level fields is {sorted(expected)}"
        )
        assert not (documented[cte_type] & top_level), (
            f"contract.md lists {sorted(documented[cte_type] & top_level)} in both the "
            f"{cte_type} row and the top-level field list"
        )


def test_documented_top_level_fields_are_typed_event_fields() -> None:
    """Guards the subtraction set the previous test leans on.

    Without this, padding the top-level list with a real KDE name would
    delete it from every row and still pass: both sides would move
    together. Anchoring the list to ``RegEngineEvent``'s typed fields --
    which is what "these come from the typed IngestEvent fields, not the
    kdes dict" claims -- stops that.
    """
    top_level = _documented_top_level_fields()

    assert top_level, "contract.md's top-level field list parsed as empty"
    unknown = sorted(top_level - set(RegEngineEvent.model_fields))
    assert not unknown, f"contract.md calls {unknown} top-level fields, but the event model has no such fields"

    # Nothing that is a typed field AND a required KDE upstream may be left
    # out: it would then have to appear in a row, contradicting the
    # document's own split.
    upstream_names = {kde for kdes in load_snapshot().table.values() for kde in kdes}
    typed_and_required = {name for name in upstream_names if name in RegEngineEvent.model_fields}
    assert typed_and_required <= top_level, (
        f"{sorted(typed_and_required - top_level)} are typed event fields required by "
        "RegEngine, but contract.md does not list them as top-level fields"
    )


def test_document_points_at_the_source_the_artifact_was_generated_from() -> None:
    """The table's authority is the artifact's provenance, so the document
    has to name the same upstream file and symbols the artifact records."""
    provenance = load_snapshot().provenance
    text = _document()

    for expected in (provenance["source_path"], provenance["source_symbol"], provenance["source_enum"]):
        assert expected in text, f"contract.md never names {expected}, which the KDE artifact came from"
    assert provenance["source_path"] == REGENGINE_SOURCE_PATH
    assert provenance["source_commit"] in text, (
        "contract.md does not record which RegEngine commit its unverified prose was last "
        "confirmed against"
    )


def test_every_upstream_path_the_document_names_is_one_the_checkout_tests_verify() -> None:
    """Keeps ``DOCUMENTED_UPSTREAM_SYMBOLS`` honest, offline.

    The checkout tests below read that mapping, not the prose, so on its own
    the mapping could drift from the document and still pass. This closes
    the loop in both directions: a RegEngine path that reappears in the
    document without being verified fails here, and so does a mapped path
    the document stopped naming.
    """
    named = set(re.findall(r"services/[\w./-]+\.py", _document()))

    assert named - QUARANTINED_UPSTREAM_PATHS == set(DOCUMENTED_UPSTREAM_SYMBOLS)
    assert QUARANTINED_UPSTREAM_PATHS <= named, (
        "the document no longer explains that the pre-split webhook_router_v2 module is gone; "
        "drop it from QUARANTINED_UPSTREAM_PATHS too"
    )


# ---------------------------------------------------------------------------
# CTE types
# ---------------------------------------------------------------------------


def test_documented_cte_types_match_the_artifact_and_this_repos_enum() -> None:
    documented = {bullet.split("`")[1] for bullet in _bullets(_section("CTE types accepted by RegEngine"))}
    artifact_types = set(load_snapshot().table)

    # `growing` is upstream's legacy back-compat member: accepted by the
    # enum, absent from the required-KDE table.
    assert documented == artifact_types | {"growing"}
    assert {cte_type.value for cte_type in CTEType} == artifact_types
    assert "growing" not in {cte_type.value for cte_type in CTEType}, (
        "contract.md says the simulator never emits `growing`, but CTEType declares it"
    )


# ---------------------------------------------------------------------------
# Endpoint and headers, against the client that actually posts
# ---------------------------------------------------------------------------


def _documented_ingest_endpoint() -> tuple[str, str]:
    for bullet in _bullets(_section("Endpoints")):
        if bullet.startswith("Ingest endpoint"):
            method, path = _backticked(bullet)[0].split(None, 1)
            return method, path
    raise AssertionError("contract.md's Endpoints section no longer names the ingest endpoint")


def test_documented_ingest_endpoint_is_the_one_the_live_client_posts_to() -> None:
    method, path = _documented_ingest_endpoint()

    assert method == "POST"
    assert path == urlparse(DEFAULT_LIVE_INGEST_ENDPOINT).path


class _RecordingClient:
    """Minimal stand-in for ``httpx.AsyncClient`` that captures one POST."""

    sent: dict[str, Any] = {}

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, endpoint: str, *, headers: dict[str, str], **_kwargs: Any) -> Any:
        type(self).sent = {"endpoint": endpoint, "headers": dict(headers)}

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"accepted": 1}

        return _Response()


def _headers_sent(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    monkeypatch.setattr("app.regengine_client.httpx.AsyncClient", _RecordingClient)
    payload = IngestPayload(
        source="contract-doc-test",
        events=[
            RegEngineEvent(
                cte_type=CTEType.HARVESTING,
                traceability_lot_code="TLC-20260427-000001",
                product_description="Romaine Lettuce",
                quantity=500,
                unit_of_measure="cases",
                location_name="Valley Fresh Farms",
                timestamp=datetime(2026, 4, 27, 8, 30, tzinfo=UTC),
                kdes={"harvest_date": "2026-04-27", "reference_document": "HARV-1"},
            )
        ],
    )
    config = SimulationConfig(
        delivery={
            "mode": "live",
            "endpoint": None,
            "api_key": "contract-doc-key",
            "tenant_id": "contract-doc-tenant",
        }
    )
    asyncio.run(LiveRegEngineClient().ingest(payload, config))
    return set(_RecordingClient.sent["headers"])


def test_documented_headers_are_the_headers_the_live_client_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """The document's header table is the integration checklist an operator
    reads before pointing the simulator at production. Pin it to the
    request the client actually builds, both on the unsigned migration ramp
    and with a shared secret configured."""
    documented = _documented_header_names()
    signature_header = "X-Webhook-Signature"
    assert signature_header in documented, "contract.md no longer documents the signature header"

    monkeypatch.delenv(WEBHOOK_HMAC_SECRET_ENV, raising=False)
    assert _headers_sent(monkeypatch) == set(documented) - {signature_header}

    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, "contract-doc-secret")
    assert _headers_sent(monkeypatch) == set(documented)


def test_documented_signature_scheme_is_the_one_the_client_computes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEBHOOK_HMAC_SECRET_ENV, "contract-doc-secret")
    _headers_sent(monkeypatch)

    value = _RecordingClient.sent["headers"]["X-Webhook-Signature"]
    scheme, _, digest = value.partition("=")

    assert f"{scheme}=<hex>" in _document(), "contract.md documents a different signature scheme"
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


# ---------------------------------------------------------------------------
# Batch and window limits, against the mock that mirrors them
# ---------------------------------------------------------------------------


def test_documented_limits_match_the_mocks_mirrored_constants() -> None:
    limits = _documented_limits()

    assert limits["events per batch"] == [MIN_BATCH_EVENTS, MAX_BATCH_EVENTS]
    assert limits["Event timestamp ceiling"] == [MAX_FUTURE_HOURS]
    assert limits["Event timestamp floor"] == [MAX_EVENT_AGE_DAYS]
    assert limits["Idempotency replay window"] == [IDEMPOTENCY_TTL_HOURS]


def test_documented_location_kdes_are_the_full_list_the_validator_checks() -> None:
    """The document used to end this list with "etc.", which is how a
    reader ends up believing a location KDE the validator ignores will
    satisfy the requirement."""
    payload_section = _section("Per-event payload fields")
    documented = set(_backticked(payload_section))

    assert set(LOCATION_KDE_FIELDS) <= documented, (
        f"contract.md omits location KDEs the validator accepts: "
        f"{sorted(set(LOCATION_KDE_FIELDS) - documented)}"
    )
    assert "etc." not in payload_section.split("location-bearing KDEs")[1].split(".")[0]


# ---------------------------------------------------------------------------
# The sample payload
# ---------------------------------------------------------------------------


def test_sample_payload_is_a_body_this_repos_event_model_accepts() -> None:
    sample = _documented_sample_payload()

    event = RegEngineEvent(**sample)

    assert event.cte_type.value in load_snapshot().table
    assert len(event.traceability_lot_code) >= 3
    assert 1 <= len(event.product_description) <= 500
    assert event.quantity > 0


def test_sample_payload_gln_carries_a_valid_gs1_check_digit() -> None:
    """RegEngine's GLN field validator reads ``STRICT_GLN_VALIDATION``,
    which defaults to true, and a strict failure raises inside Pydantic --
    so a bad check digit in the documented sample is not a cosmetic typo,
    it is a body that 422s the whole batch. The value this replaced
    (0850000001001) was exactly that."""
    gln = _documented_sample_payload()["location_gln"]

    assert re.fullmatch(r"\d{13}", gln), f"documented sample GLN {gln!r} is not 13 digits"
    assert int(gln[-1]) == gs1_check_digit(gln[:-1]), (
        f"documented sample GLN {gln} has check digit {gln[-1]}, expected {gs1_check_digit(gln[:-1])}"
    )


def test_sample_payload_names_a_location_the_simulator_actually_uses() -> None:
    """And with the GLN that location really carries -- the pairing is what
    makes the sample copy-pasteable against a running simulator."""
    from app.scenarios import SCENARIO_PRESETS, ScenarioId

    sample = _documented_sample_payload()
    locations = {
        location.name: location.gln
        for location in SCENARIO_PRESETS[ScenarioId.LEAFY_GREENS_SUPPLIER].farms
    }

    assert sample["location_name"] in locations, (
        f"contract.md's sample location {sample['location_name']!r} is not a scenario location"
    )
    assert sample["location_gln"] == locations[sample["location_name"]]


# ---------------------------------------------------------------------------
# Export columns
# ---------------------------------------------------------------------------


def test_documented_export_columns_are_the_columns_the_exporter_writes() -> None:
    documented = _bullets(_section("Mock export columns expected by this repo"))

    assert documented == list(FDA_EXPORT_COLUMNS)


# ---------------------------------------------------------------------------
# Against a real RegEngine checkout (opt-in)
# ---------------------------------------------------------------------------

pytestmark_checkout = pytest.mark.skipif(
    not os.getenv(REGENGINE_CHECKOUT_ENV),
    reason=f"set {REGENGINE_CHECKOUT_ENV} to a RegEngine checkout to check contract.md against upstream",
)


def _checkout() -> Path:
    return Path(os.environ[REGENGINE_CHECKOUT_ENV])


def _model_field_constraints(source: str, model: str) -> dict[str, dict[str, Any]]:
    """``{field: {keyword: literal}}`` for a pydantic model's ``Field(...)``
    calls, read with ``ast`` rather than by importing RegEngine."""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != model:
            continue
        fields: dict[str, dict[str, Any]] = {}
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            call = statement.value
            if not isinstance(call, ast.Call):
                continue
            constraints: dict[str, Any] = {}
            for keyword in call.keywords:
                if keyword.arg and isinstance(keyword.value, ast.Constant):
                    constraints[keyword.arg] = keyword.value.value
            fields[statement.target.id] = constraints
        return fields
    raise AssertionError(f"RegEngine's source has no `class {model}`")


@pytestmark_checkout
def test_every_upstream_path_the_document_names_exists() -> None:
    missing = [path for path in DOCUMENTED_UPSTREAM_SYMBOLS if not (_checkout() / path).is_file()]

    assert not missing, f"contract.md names RegEngine paths that do not exist: {missing}"


@pytestmark_checkout
def test_every_upstream_symbol_lives_where_the_document_says() -> None:
    """The check that would have caught #211's stale citations: the
    document attributed ``_verify_webhook_signature`` and the idempotency
    dependency to ``webhook_router_v2.py``, a module upstream had already
    split into a package."""
    problems: list[str] = []
    for path, symbols in DOCUMENTED_UPSTREAM_SYMBOLS.items():
        source = (_checkout() / path).read_text(encoding="utf-8")
        problems.extend(f"{symbol} is not in {path}" for symbol in symbols if symbol not in source)

    assert not problems, problems


@pytestmark_checkout
def test_webhook_router_v2_is_a_package_as_the_document_states() -> None:
    ingestion_app = _checkout() / "services" / "ingestion" / "app"

    assert (ingestion_app / "webhook_router_v2").is_dir()
    assert not (ingestion_app / "webhook_router_v2.py").exists()


@pytestmark_checkout
def test_documented_payload_constraints_match_regengines_models() -> None:
    source = (_checkout() / REGENGINE_SOURCE_PATH).read_text(encoding="utf-8")
    payload_text = _section("Per-event payload fields")

    event_fields = _model_field_constraints(source, "IngestEvent")
    assert f"`min_length={event_fields['traceability_lot_code']['min_length']}`" in payload_text
    product = event_fields["product_description"]
    assert f"`min_length={product['min_length']}, max_length={product['max_length']}`" in payload_text
    assert f"`gt={event_fields['quantity']['gt']}`" in payload_text

    events_field = _model_field_constraints(source, "WebhookPayload")["events"]
    limits = _documented_limits()
    assert limits["events per batch"] == [events_field["min_length"], events_field["max_length"]]


@pytestmark_checkout
def test_documented_windows_match_regengines_route_level_defaults() -> None:
    security = (_checkout() / "services" / "ingestion" / "app" / "webhook_router_v2" / "security.py").read_text(
        encoding="utf-8"
    )
    limits = _documented_limits()

    def _env_default(name: str) -> int:
        match = re.search(rf'getenv\(\s*"{name}"\s*,\s*"(\d+)"\s*\)', security)
        assert match, f"{name} no longer has a literal default in RegEngine's security module"
        return int(match.group(1))

    assert limits["Event timestamp floor"] == [_env_default("WEBHOOK_MAX_EVENT_AGE_DAYS")]
    assert limits["Event timestamp ceiling"] == [_env_default("WEBHOOK_MAX_EVENT_FUTURE_HOURS")]


@pytestmark_checkout
def test_regengine_rejects_rather_than_flags_events_below_the_floor() -> None:
    """The document says stale events are rejected with "replay window
    exceeded" and that no ``_historical_warning`` exists. Both halves are
    checkable in the checkout, and both were wrong before #211."""
    ingestion_app = _checkout() / "services" / "ingestion" / "app"
    security = (ingestion_app / "webhook_router_v2" / "security.py").read_text(encoding="utf-8")
    routes = (ingestion_app / "webhook_router_v2" / "routes.py").read_text(encoding="utf-8")

    # Upstream splits the message across f-string fragments, so match the
    # phrase loosely rather than pinning its line wrapping.
    assert "replay window" in security
    assert "WEBHOOK_MAX_EVENT_AGE_DAYS" in security
    assert "_validate_event_timestamp_window" in routes
    assert 'status="rejected"' in routes

    implementations = [
        path
        for path in ingestion_app.rglob("*.py")
        if re.search(r"^\s*[^#\n]*_historical_warning", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert not implementations, (
        f"_historical_warning is implemented after all, in {[str(p) for p in implementations]}; "
        "contract.md says it does not exist"
    )


@pytestmark_checkout
def test_documented_top_level_fields_are_what_upstream_merges_into_the_lookup() -> None:
    """RegEngine's ``_validate_event_kdes`` builds its lookup from a literal
    dict of typed fields plus ``**event.kdes``. The document's top-level
    list has to be exactly those literal keys."""
    validation = (
        _checkout() / "services" / "ingestion" / "app" / "webhook_router_v2" / "validation.py"
    ).read_text(encoding="utf-8")

    tree = ast.parse(validation)
    merged: set[str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_event_kdes":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict) and any(key is None for key in sub.keys):
                    merged = {
                        key.value
                        for key in sub.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
                    break
    assert merged is not None, "could not find the merged KDE lookup in _validate_event_kdes"
    assert merged == _documented_top_level_fields()

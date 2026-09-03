"""Structural invariants of the split operator console (issues #154, #155).

``app/static/app.js`` used to be one 1,830-line script. It is now a set of
native ES modules loaded without a build step, which moves a class of mistakes
from "the browser silently breaks" to "nothing catches it": a typo'd import
path, a module dropped into a subdirectory where the sibling behaviour suite's
``*.js`` glob cannot see it, or a fresh unescaped ``${...}`` in a panel that no
longer lives in app.js.

``tests/test_console_behavior.py`` pins what the console *does* across every
module as one corpus. This module pins the module layout that keeps that
corpus, and the browser, honest.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.regengine_client import DEFAULT_LIVE_INGEST_ENDPOINT


client = TestClient(app)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
MODULES = sorted(STATIC.glob("*.js"))

IMPORT = re.compile(r"^import\s+(?:\{[^}]*\}\s+from\s+)?'([^']+)';$", re.M)


def test_the_console_ships_as_several_modules():
    """#154: the seams app.js mixed together are real files now."""
    names = {path.name for path in MODULES}
    assert len(names) >= 6, names
    # The seams the issue names, by the concern each one owns.
    for expected in ("app.js", "api.js", "dom.js", "actions.js", "snapshot.js", "ui.js"):
        assert expected in names, names
    # app.js is wiring only; the behaviour lives in the modules it imports.
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert len(app_js.splitlines()) < 250
    assert len(IMPORT.findall(app_js)) >= 5


def test_every_module_import_resolves_to_a_shipped_file():
    """A typo'd specifier is a blank console, and nothing else would catch it."""
    for path in MODULES:
        for specifier in IMPORT.findall(path.read_text(encoding="utf-8")):
            assert specifier.startswith("./"), (path.name, specifier)
            target = (path.parent / specifier).resolve()
            assert target.is_file(), f"{path.name} imports missing {specifier}"


def test_console_scripts_stay_directly_under_static():
    """The behaviour suite globs ``static/*.js``; a subdirectory escapes it."""
    nested = [path for path in STATIC.rglob("*.js") if path.parent != STATIC]
    assert nested == [], nested


def test_index_loads_the_console_as_a_module_with_a_versioned_url():
    tag = re.search(r"<script[^>]*app\.js[^>]*>", INDEX_HTML)
    assert tag, "index.html does not load app.js"
    assert 'type="module"' in tag.group(0), tag.group(0)
    assert re.search(r"app\.js\?v=\d+", tag.group(0)), tag.group(0)


def test_no_module_interpolates_unescaped_into_an_html_attribute():
    """#153, per file: the whole-corpus check cannot name the offender."""
    for path in MODULES:
        offenders = re.findall(
            r"=\"\$\{(?!escapeHtml\()[^}]*\}\"", path.read_text(encoding="utf-8")
        )
        assert offenders == [], (path.name, offenders)


def test_every_module_parses():
    """CI gates on ``node --check``; run it here too when node is available."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the local toolchain
        return
    for path in MODULES:
        result = subprocess.run(  # nosec B603
            [node, "--check", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_the_default_ingest_endpoint_is_not_copied_into_the_console():
    """#155: the URL lives in Python only; the console is served it."""
    for path in (*MODULES, STATIC / "index.html"):
        assert DEFAULT_LIVE_INGEST_ENDPOINT not in path.read_text(encoding="utf-8"), path.name
    body = client.get("/api/integration/status").json()
    assert body["default_endpoint"] == DEFAULT_LIVE_INGEST_ENDPOINT
    # The console adopts whatever that response carries, including as the
    # endpoint field's placeholder, which index.html used to hard-code.
    config_js = (STATIC / "config.js").read_text(encoding="utf-8")
    assert "integration?.default_endpoint" in config_js
    assert "ids.endpoint.placeholder = value;" in config_js
    assert 'placeholder=""' not in INDEX_HTML


def test_action_handlers_share_one_failure_reporter():
    """#154: the 21 identical catch blocks collapse to reportActionError()."""
    actions = (STATIC / "actions.js").read_text(encoding="utf-8")
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    assert "function reportActionError(error)" in ui
    assert "runWithBusy(button, handler).catch(reportActionError);" in ui
    # Twelve handlers, and only testConnection still catches (it also has to
    # paint #connectionResult) — and it reports through the shared helper.
    assert len(re.findall(r"^export async function \w+\(", actions, re.M)) == 12
    assert actions.count("} catch (error) {") == 1
    assert actions.count("reportActionError(error);") == 1
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in MODULES)
    assert corpus.count("setStatus(error.message, 'error', 5000);") <= 3


def test_the_connection_probe_verdicts_all_carry_a_tone():
    """A verdict with no tone renders neutral grey, however broken it is."""
    labels = (STATIC / "labels.js").read_text(encoding="utf-8")
    tones = re.search(r"CONNECTION_VERDICT_TONES = \{(.*?)\n\};", labels, re.S)
    assert tones, "CONNECTION_VERDICT_TONES not found"
    mapped = dict(re.findall(r"(\w+): '(\w+)'", tones.group(1)))
    assert mapped["signature_mismatch"] == "error"
    assert mapped["contract_mismatch"] == "error"
    assert mapped["mock"] == "success"

"""Regression coverage for refresh() error handling in app/static/app.js.

There is no JS test runner in this repo (see tests/test_app_js_escaping.py
for the established precedent), so this test inspects the served source
directly: it pins down that refresh() catches its own failures and reports
them via setStatus(), the same convention every other bound action function
(startLoop, stopLoop, stepOnce, ...) follows.

Previously refresh() awaited its api() calls with no surrounding try/catch,
and bindAsyncClick()/runWithBusy() never attached a .catch() either, so a
transient failure while clicking Refresh produced a silent unhandled promise
rejection instead of a visible status message.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).parent.parent / "app" / "static" / "app.js"


def _app_js_source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    """Return the source text of a top-level `function <name>(...)` block.

    Good enough for this file's flat, unnested top-level function layout:
    it slices from the function's own signature to the next top-level
    `function` declaration (or end of file).
    """
    start = source.index(f"function {function_name}(")
    next_function = re.compile(r"^(async )?function \w+\(", re.MULTILINE)
    match = next_function.search(source, start + 1)
    end = match.start() if match else len(source)
    return source[start:end]


def test_refresh_wraps_its_body_in_try_catch():
    body = _function_body(_app_js_source(), "refresh")
    assert "try {" in body
    assert "} catch (error) {" in body


def test_refresh_reports_failures_through_set_status():
    body = _function_body(_app_js_source(), "refresh")
    catch_block = body.split("} catch (error) {", 1)[1]
    assert "setStatus(error.message, 'error', 5000)" in catch_block


def test_refresh_still_renders_the_snapshot_on_success():
    body = _function_body(_app_js_source(), "refresh")
    assert "renderSnapshot(status, events.events, health)" in body

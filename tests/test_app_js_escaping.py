"""Regression coverage for HTML-attribute escaping in app/static/app.js.

There is no JS test runner in this repo, so these tests inspect the served
source directly: they pin down that every value interpolated into an HTML
attribute inside a template literal goes through ``escapeHtml`` first. This
guards the readiness banner's ``data-tone`` attribute in particular, which
historically interpolated ``readiness.tone`` unescaped while every
neighbouring field in the same template was escaped.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).parent.parent / "app" / "static" / "app.js"

# Matches `data-something="${...}"` inside a template literal and captures
# the interpolated expression, so it can be checked for an escapeHtml(...)
# wrapper.
ATTRIBUTE_INTERPOLATION = re.compile(r'data-[\w-]+="\$\{([^}]*)\}"')


def _app_js_source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    """Return the source text of a top-level `function <name>(...)` block.

    Good enough for this file's flat, unnested top-level function layout:
    it slices from the function's own signature to the next top-level
    `function` declaration (or end of file).
    """
    start = source.index(f"function {function_name}(")
    next_function = re.compile(r"^function \w+\(", re.MULTILINE)
    match = next_function.search(source, start + 1)
    end = match.start() if match else len(source)
    return source[start:end]


def test_readiness_tone_is_escaped_before_reaching_the_data_tone_attribute():
    source = _app_js_source()
    assert 'data-tone="${escapeHtml(readiness.tone)}"' in source
    assert 'data-tone="${readiness.tone}"' not in source


def test_readiness_banner_has_no_unescaped_attribute_interpolation():
    banner_body = _function_body(_app_js_source(), "renderReadinessBanner")
    unescaped = [
        expr.strip()
        for expr in ATTRIBUTE_INTERPOLATION.findall(banner_body)
        if not expr.strip().startswith("escapeHtml(")
    ]
    assert unescaped == []

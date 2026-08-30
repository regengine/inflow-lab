"""No unescaped interpolation may land inside an HTML attribute in app.js.

Regression cover for #153. `renderReadinessBanner()` interpolated the
server-sourced `readiness.tone` straight into `data-tone="..."` while every
neighbouring field went through `escapeHtml()` -- the one silent exception to
the file's escape-everything convention, and one widening of `tone`'s source
away from an attribute breakout.

This is a static check rather than a browser test on purpose: the point is the
convention holding across the whole file, not one call site behaving today.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"

# An attribute value in a template literal: name="...${...}...".
_ATTRIBUTE = re.compile(r'([a-zA-Z-]+)="([^"]*\$\{[^"]*)"')
_INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")

# A ternary whose branches are both string literals interpolates a value the
# file itself chose, never anything that came off the wire -- e.g.
# `${warningCount ? ' has-warning' : ''}`. Those are safe by construction.
_LITERAL_TERNARY = re.compile(r"^[^?]*\?\s*'[^']*'\s*:\s*'[^']*'$")


def _unescaped_attribute_interpolations() -> list[str]:
    source = APP_JS.read_text(encoding="utf-8")
    offenders: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for _attribute, value in _ATTRIBUTE.findall(line):
            for expression in _INTERPOLATION.findall(value):
                expression = expression.strip()
                if expression.startswith("escapeHtml("):
                    continue
                if _LITERAL_TERNARY.match(expression):
                    continue
                offenders.append(f"{APP_JS.name}:{line_number} {_attribute}=\"${{{expression}}}\"")
    return offenders


def test_no_unescaped_interpolation_inside_an_html_attribute():
    assert _unescaped_attribute_interpolations() == []


def test_readiness_tone_is_escaped():
    source = APP_JS.read_text(encoding="utf-8")

    assert 'data-tone="${escapeHtml(readiness.tone)}"' in source
    assert 'data-tone="${readiness.tone}"' not in source


def test_the_check_would_catch_a_regression():
    # Guards the guard: if the scanner stopped matching, the two tests above
    # would pass vacuously against a file that had regressed.
    offenders = []
    for _attribute, value in _ATTRIBUTE.findall('<div data-tone="${readiness.tone}">'):
        offenders.extend(_INTERPOLATION.findall(value))

    assert offenders == ["readiness.tone"]
    assert not _LITERAL_TERNARY.match("readiness.tone")

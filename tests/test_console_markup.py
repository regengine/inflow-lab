"""No unescaped interpolation may land inside an HTML attribute.

Regression cover for #153. `renderReadinessBanner()` interpolated the
server-sourced `readiness.tone` straight into `data-tone="..."` while every
neighbouring field went through `escapeHtml()` -- the one silent exception to
the file's escape-everything convention, and one widening of `tone`'s source
away from an attribute breakout.

This is a static check rather than a browser test on purpose: the point is the
convention holding everywhere, not one call site behaving today. It scans every
console module, so a new one cannot quietly opt out.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"
CONSOLE_MODULES = sorted(STATIC_DIR.glob("*.js"))

# An attribute value in a template literal: name="...${...}...".
_ATTRIBUTE = re.compile(r'([a-zA-Z-]+)="([^"]*\$\{[^"]*)"')
_INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")

# A ternary whose branches are both string literals interpolates a value the
# file itself chose, never anything that came off the wire -- e.g.
# `${warningCount ? ' has-warning' : ''}`. Those are safe by construction, and
# so is a chain of them (`a ? 'x' : b ? 'y' : ''`): every branch is still a
# literal the file wrote, so no wire value can reach the attribute.
_LITERAL_STRING = r"'[^']*'"
_LITERAL_TERNARY = re.compile(
    r"^[^?']*\?\s*" + _LITERAL_STRING + r"\s*:\s*(?:" + _LITERAL_STRING
    + r"|[^?']*\?\s*" + _LITERAL_STRING + r"\s*:\s*" + _LITERAL_STRING + r")$"
)


def _unescaped_attribute_interpolations() -> list[str]:
    offenders: list[str] = []
    for module in CONSOLE_MODULES:
        source = module.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            for _attribute, value in _ATTRIBUTE.findall(line):
                for expression in _INTERPOLATION.findall(value):
                    expression = expression.strip()
                    if expression.startswith("escapeHtml("):
                        continue
                    if _LITERAL_TERNARY.match(expression):
                        continue
                    offenders.append(
                        f"{module.name}:{line_number} {_attribute}=\"${{{expression}}}\""
                    )
    return offenders


def _console_source() -> str:
    return "\n".join(module.read_text(encoding="utf-8") for module in CONSOLE_MODULES)


def test_no_unescaped_interpolation_inside_an_html_attribute():
    assert _unescaped_attribute_interpolations() == []


def test_readiness_tone_is_escaped():
    source = _console_source()

    assert 'data-tone="${escapeHtml(readiness.tone)}"' in source
    assert 'data-tone="${readiness.tone}"' not in source


def test_every_console_module_is_scanned():
    # Guards against the glob silently matching nothing after a rename. The
    # console is currently the single `app.js` script that index.html loads;
    # when #154's module split is re-landed, extend this set to the new
    # module names so a renamed file cannot silently drop out of the scan.
    assert {module.name for module in CONSOLE_MODULES} >= {"app.js"}


def test_the_check_would_catch_a_regression():
    # Guards the guard: if the scanner stopped matching, the two tests above
    # would pass vacuously against a file that had regressed.
    offenders = []
    for _attribute, value in _ATTRIBUTE.findall('<div data-tone="${readiness.tone}">'):
        offenders.extend(_INTERPOLATION.findall(value))

    assert offenders == ["readiness.tone"]
    assert not _LITERAL_TERNARY.match("readiness.tone")

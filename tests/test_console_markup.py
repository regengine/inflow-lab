"""No unescaped interpolation may land inside an HTML attribute.

Regression cover for #153. `renderReadinessBanner()` interpolated the
server-sourced `readiness.tone` straight into `data-tone="..."` while every
neighbouring field went through `escapeHtml()` -- the one silent exception to
the file's escape-everything convention, and one widening of `tone`'s source
away from an attribute breakout.

This is a static check rather than a browser test on purpose: the point is the
convention holding everywhere, not one call site behaving today. It scans every
console module, so a new one cannot quietly opt out.

The #217 accessibility pins at the bottom live here for the same reason: this
is the one test module that already reads app/static, and the properties they
guard -- which live regions announce atomically, and that an error escalates
its region to assertive -- are markup facts, not behaviour a browser has to be
driven to observe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"
CONSOLE_MODULES = sorted(STATIC_DIR.glob("*.js"))
INDEX_HTML = STATIC_DIR / "index.html"

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


# ---------------------------------------------------------------------------
# #217 -- the console's live regions, pinned as markup.
# ---------------------------------------------------------------------------


def _opening_tag(element_id: str) -> str:
    """The `<... id="element_id" ...>` opening tag from index.html, attributes included."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r'<[^<>]*\bid="' + re.escape(element_id) + r'"[^<>]*>', source)
    assert match is not None, f'index.html has no element with id="{element_id}"'
    return match.group(0)


@pytest.mark.parametrize("element_id", ["statusMessage", "deliverySummary"])
def test_live_regions_the_console_rewrites_wholesale_announce_atomically(element_id):
    # Both regions are replaced as a unit on every update (setStatus() and the
    # delivery summary render). Without aria-atomic="true" a screen reader
    # announces only the text nodes that changed, which for a sentence
    # rewritten in place is a fragment with no subject.
    tag = _opening_tag(element_id)

    assert 'aria-live="' in tag, f"#{element_id} is not a live region"
    assert 'aria-atomic="true"' in tag


def test_an_error_status_escalates_the_live_region_to_assertive():
    # setStatus() promotes #statusMessage from the polite default the markup
    # declares to assertive for an error tone: an error interrupts, everything
    # else waits its turn. Pinned as the literal expression so the escalation
    # cannot quietly become unconditional in either direction.
    assert "tone === 'error' ? 'assertive' : 'polite'" in _console_source()

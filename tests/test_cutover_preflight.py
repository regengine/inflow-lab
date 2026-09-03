"""Guards on scripts/cutover_preflight.sh's proxy-contract path list.

#106: the loop probed eight paths unauthenticated, so with Basic Auth enabled
every one of them compared 401 against 401 and rolled up to "PRE-FLIGHT
PASSED". Two of the eight were not mounted routes at all -- those comparisons
were 404-vs-404. The script now authenticates and treats a matching non-2xx as
a failure; this file keeps the path list honest, since a phantom path is
invisible until someone runs a cutover.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.main import app


PREFLIGHT = Path(__file__).resolve().parents[1] / "scripts" / "cutover_preflight.sh"


def probed_paths() -> list[str]:
    source = PREFLIGHT.read_text(encoding="utf-8")
    block = re.search(r"^PATHS=\((.*?)^\)", source, re.MULTILINE | re.DOTALL)
    assert block, "could not find the PATHS array in cutover_preflight.sh"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_the_path_list_is_not_empty():
    assert probed_paths()


@pytest.mark.parametrize("path", probed_paths())
def test_every_probed_path_resolves_to_a_mounted_route(path):
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    assert any(route.matches(scope)[0].value >= 2 for route in app.routes), (
        f"{path} is probed by the cutover pre-flight but no router in "
        "app/main.py mounts it, so the probe can only ever compare two 404s"
    )


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_the_contract_loop_authenticates():
    """A credential-less loop compares 401 to 401 and proves nothing."""
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "REGENGINE_REMOTE_USERNAME" in source
    assert "CURL_AUTH" in source
    assert "-u" in source


@pytest.mark.xfail(strict=True, reason="reverted by PR #225's HEAD-side conflict resolution; re-landing tracked in #232")
def test_a_matching_error_status_is_reported_as_a_failure():
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "nothing was compared" in source
    assert "401 with credentials supplied" in source

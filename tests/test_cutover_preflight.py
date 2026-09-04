"""Guards on scripts/cutover_preflight.sh's proxy-contract path list.

#106: the loop probed eight paths unauthenticated, so with Basic Auth enabled
every one of them compared 401 against 401 and rolled up to "PRE-FLIGHT
PASSED". Two of the eight were not mounted routes at all -- those comparisons
were 404-vs-404. The script now authenticates and treats a matching non-2xx as
a failure; this file keeps the path list honest, since a phantom path is
invisible until someone runs a cutover.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404 -- runs the repo's own pre-flight script
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


def test_the_contract_loop_authenticates():
    """A credential-less loop compares 401 to 401 and proves nothing (#106).

    Asserted by RUNNING the script rather than by grepping it for one
    implementation's variable name. The previous version required the literal
    `CURL_AUTH`, which is one way to hold the credentials; this one holds them
    in CRED_USER/CRED_PASS and passes them with `curl -u`. Both satisfy #106,
    and only the behaviour is worth pinning: with no credentials the script
    must refuse to report the contract section as verified.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["bash", str(PREFLIGHT), "https://old.invalid", "https://new.invalid"],
        capture_output=True,
        text=True,
        timeout=120,
        env={k: v for k, v in os.environ.items()
             if k not in {"REGENGINE_REMOTE_USERNAME", "REGENGINE_REMOTE_PASSWORD"}},
    )
    combined = result.stdout + result.stderr

    assert "REGENGINE_REMOTE_USERNAME" in combined, (
        "running without credentials must say which ones are missing"
    )
    assert "PRE-FLIGHT PASSED" not in combined, (
        "a run that compared nothing must never roll up to PASSED"
    )
    assert result.returncode != 0, "an unverifiable pre-flight must not exit 0"

    # And the credentials must actually reach curl, not merely be named.
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "-u" in source and "REGENGINE_REMOTE_USERNAME" in source


def test_a_matching_error_status_is_reported_as_a_failure():
    """Two services agreeing on an error is not two services agreeing (#106).

    The rolled-up verdict is the property: a probe that came back non-2xx from
    the old service -- 401 for stale credentials, 404 for a path no router
    mounts, 000 for unreachable -- must not count as a match, however
    identically the new service failed.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["bash", str(PREFLIGHT), "https://old.invalid", "https://new.invalid"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ,
             "REGENGINE_REMOTE_USERNAME": "preflight-test",
             "REGENGINE_REMOTE_PASSWORD": "preflight-test"},
    )
    combined = result.stdout + result.stderr

    # Both hosts are unresolvable, so every probe is 000 on both sides -- the
    # purest form of "identical, and identically useless".
    assert "PRE-FLIGHT PASSED" not in combined, (
        "matching failures on both services were rolled up as a pass"
    )
    assert result.returncode != 0

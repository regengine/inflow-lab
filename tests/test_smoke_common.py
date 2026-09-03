"""The shared smoke-test harness the scripts now use instead of four copies.

`remote_smoke.py`, `live_trial.py`, `smoke_regression.py` and
`run_full_fsma_simulation.py` each carried their own copy of these helpers,
and `browser_smoke.py` copied `remote_smoke.py`'s SHA comparison verbatim
(#139). The behaviour is unchanged by the consolidation -- what these tests
add is direct coverage of the single implementation, plus a guard on the
import path, since a shared module is only shared if every script can
actually reach it when run the way its runbook says to run it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from scripts._smoke_common import (
    assert_status,
    response_json,
    secret_values,
    sha_prefix_match,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SMOKE_SCRIPTS = [
    "scripts/remote_smoke.py",
    "scripts/live_trial.py",
    "scripts/smoke_regression.py",
    "scripts/browser_smoke.py",
    "run_full_fsma_simulation.py",
]


class _Failure(AssertionError):
    pass


def _response(status_code: int = 200, *, text: str = "", json_body=None) -> httpx.Response:
    request = httpx.Request("GET", "https://demo.example.test/api/health")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, text=text, request=request)


@pytest.mark.parametrize("script", SMOKE_SCRIPTS)
def test_every_smoke_script_imports_from_an_unrelated_cwd(script, tmp_path):
    """Each script must reach scripts/_smoke_common when run as a file.

    Only the script's own directory goes on sys.path when Python runs a
    file, so a script living in scripts/ cannot import the `scripts`
    package unless it puts the repo root there itself. Running from an
    unrelated working directory is what exposes that -- and running from
    somewhere else is normal: DEPLOYMENT_PROFILES.md and the HMAC runbook
    both drive these from an operator's shell.
    """
    path = REPO_ROOT / script
    # Reproduce what the CLI does when it runs a file: the script's own
    # directory becomes sys.path[0], and the working directory is not on
    # sys.path at all. run_name keeps the __main__ guard from firing, so
    # this exercises the import path without running the smoke itself.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, runpy; sys.path.insert(0, {str(path.parent)!r}); "
            f"runpy.run_path({str(path)!r}, run_name='imported_for_test')",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, f"{script} failed to import:\n{result.stderr}"


def test_sha_prefix_match_accepts_either_side_being_the_short_one():
    full = "abcdef1234567890abcdef1234567890abcdef12"

    assert sha_prefix_match(full, "abcdef123456") is True
    assert sha_prefix_match("abcdef123456", full) is True
    assert sha_prefix_match("  ABCDEF123456  ", full) is True
    assert sha_prefix_match(full, "0000000000") is False


def test_secret_values_collects_credential_named_env_vars(monkeypatch):
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "a-real-password")
    monkeypatch.setenv("SOME_API_KEY", "another-secret-value")
    monkeypatch.setenv("REGENGINE_REMOTE_TENANT", "not-a-credential")
    monkeypatch.setenv("SHORT_TOKEN", "ab")

    values = secret_values("explicitly-passed-secret")

    assert "a-real-password" in values
    assert "another-secret-value" in values
    assert "explicitly-passed-secret" in values
    assert "not-a-credential" not in values
    # Too short to blank out without mangling unrelated output.
    assert "ab" not in values


def test_response_json_redacts_a_non_json_body():
    response = _response(200, text="<html>password=hunter2-the-password</html>")

    with pytest.raises(_Failure) as exc:
        response_json(
            _Failure,
            response,
            "/api/health",
            redact=lambda value: value.replace("hunter2-the-password", "[redacted]"),
        )

    assert "hunter2-the-password" not in str(exc.value)
    assert "[redacted]" in str(exc.value)


def test_response_json_rejects_a_non_object_body():
    with pytest.raises(_Failure, match="expected JSON object response"):
        response_json(_Failure, _response(200, json_body=[1, 2, 3]), "/api/events")


def test_response_json_checks_the_status_first():
    with pytest.raises(_Failure, match="expected HTTP 200, got 503"):
        response_json(_Failure, _response(503, json_body={"ok": False}), "/api/health")


def test_assert_status_redacts_the_error_body():
    response = _response(401, text="rejected key rge_live_supersecret")

    with pytest.raises(_Failure) as exc:
        assert_status(
            _Failure,
            response,
            200,
            "/api/events",
            redact=lambda value: value.replace("rge_live_supersecret", "[redacted]"),
        )

    assert "rge_live_supersecret" not in str(exc.value)

"""The two manual release-gate scripts must honor REGENGINE_DATA_DIR (#108).

Both build a TestClient over the imported app, so the data root they write to
is whatever ``app.tenancy`` resolved from the *caller's* environment. Both
used to compare against, and clean up, a hardcoded ``data/`` instead -- so run
from a shell carrying ``REGENGINE_DATA_DIR`` (which the shared-demo and
live-trial profiles in DEPLOYMENT_PROFILES.md both export) the release smoke
failed on a literal path-string mismatch and both scripts deleted a directory
the app had never written, accumulating test tenants in the real store.

These run each script as a subprocess with the variable set, because the
defect only exists across that boundary: the data root is read at import time,
so nothing about it can be exercised by monkeypatching inside a session that
has already imported the app.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_release_script(script: str, data_root: Path) -> subprocess.CompletedProcess[str]:
    data_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["REGENGINE_DATA_DIR"] = str(data_root)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_smoke_regression_passes_under_a_relocated_data_root(tmp_path):
    data_root = tmp_path / "release-smoke-root"

    result = _run_release_script("scripts/smoke_regression.py", data_root)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Release smoke regression passed." in result.stdout


def test_smoke_regression_cleans_up_under_the_configured_data_root(tmp_path):
    data_root = tmp_path / "release-smoke-root"

    result = _run_release_script("scripts/smoke_regression.py", data_root)

    # The tenants the smoke provisions must be removed from the root the app
    # actually wrote to, not from ./data. Checked independently of the exit
    # code above: a cleanup aimed at the wrong directory leaves these behind
    # whether the run passed or failed.
    leftover = sorted(path.name for path in (data_root / "tenants").glob("release-smoke-*"))
    assert leftover == [], f"tenants left behind: {leftover}\nstdout:\n{result.stdout}"


def test_full_fsma_simulation_cleans_up_under_the_configured_data_root(tmp_path):
    data_root = tmp_path / "golden-path-root"

    result = _run_release_script("run_full_fsma_simulation.py", data_root)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Golden path complete" in result.stdout
    assert not (data_root / "tenants" / "golden-path-demo").exists()

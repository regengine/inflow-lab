"""Coverage for the two scripts that make up the manual release gate.

`scripts/smoke_regression.py` and `run_full_fsma_simulation.py` were the only
substantial code in the repo with no automated coverage, which is how both came
to hardcode `data/` while the app resolves its root from REGENGINE_DATA_DIR:
the smoke failed a literal path assertion, and both deleted a directory they
had never written to. These tests run each script against a scratch data root
and assert it leaves nothing behind there.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import tenancy
from app.main import app
from app.tenancy import DEFAULT_TENANT_ID


@pytest.fixture
def scratch_data_root(monkeypatch, tmp_path):
    """Repoint tenant storage at an empty root, as REGENGINE_DATA_DIR would.

    Patching the module attributes rather than the env var because
    `app.tenancy` resolves DATA_ROOT once at import; a test that only set the
    variable would be checking nothing.
    """
    tenant_root = tmp_path / "volume" / "tenants"
    tenant_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tenancy, "DATA_ROOT", tmp_path / "volume")
    monkeypatch.setattr(tenancy, "TENANT_DATA_ROOT", tenant_root)
    monkeypatch.setattr(
        tenancy, "_tenant_controllers", {DEFAULT_TENANT_ID: tenancy.controller}
    )
    monkeypatch.setattr(tenancy, "_tenants_being_deleted", {})
    return tenant_root


def test_release_smoke_passes_against_a_relocated_data_root(scratch_data_root):
    """The whole release gate, run where REGENGINE_DATA_DIR is not `data`."""
    from scripts.smoke_regression import TENANTS, run_smoke

    with TestClient(app) as client:
        run_smoke(client)

    # run_smoke itself does not clean up; main() does. What matters here is
    # that the tenants were created under the configured root at all -- the
    # persist_path assertion inside run_smoke is what used to fail.
    assert {path.name for path in scratch_data_root.iterdir()} >= set(TENANTS)


def test_release_smoke_cleanup_removes_tenants_from_the_configured_root(
    scratch_data_root,
):
    from scripts.smoke_regression import TENANTS, cleanup_smoke_tenants, run_smoke

    with TestClient(app) as client:
        run_smoke(client)
    cleanup_smoke_tenants()

    leftovers = {path.name for path in scratch_data_root.iterdir()}
    assert not (leftovers & set(TENANTS)), (
        "release-smoke tenants were left behind in the configured data root; "
        "cleanup is deleting some other directory"
    )


def test_full_fsma_simulation_cleans_up_its_tenant(scratch_data_root, capsys):
    import run_full_fsma_simulation as sim

    assert sim.main() == 0
    captured = capsys.readouterr()
    assert "Golden path complete" in captured.out
    assert sim.TENANT_ID not in {path.name for path in scratch_data_root.iterdir()}


def test_no_script_hardcodes_the_default_data_root():
    """Acceptance criterion from #108, kept as a guard against reintroduction."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    for relative in ("scripts/smoke_regression.py", "run_full_fsma_simulation.py"):
        source = (repo_root / relative).read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            code = line.split("#", 1)[0]
            assert '"data/' not in code and "'data/" not in code, (
                f"{relative}:{lineno} hardcodes the default data root; derive it "
                "from app.tenancy instead"
            )

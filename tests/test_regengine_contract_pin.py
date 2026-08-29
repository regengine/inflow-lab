"""Pin app.cte_rules.REQUIRED_KDES to RegEngine's live webhook contract.

The expected table is NOT typed out here. It is read from
``tests/data/regengine_required_kdes.json``, which
``scripts/regengine_kde_contract.py`` generates by parsing
``REQUIRED_KDES_BY_CTE`` out of RegEngine's
``services/ingestion/app/webhook_models.py`` -- the source of truth for
live ingest validation. The file records the repository, path, symbol and
commit it came from, plus a sha256 over the table.

That indirection is the point (#104). This module used to hold a second
hand-typed copy of the table under a comment asserting it had been
"copied verbatim from RegEngine". Both sides of the comparison lived in
this repo, so the test could only ever prove this repo agreed with
itself: RegEngine-side drift was invisible to it, and the claim in the
comment was unfalsifiable.

What now checks the copy is real:

* ``tests/test_contract_provenance.py`` verifies the recorded sha256
  against the file's own contents, so the snapshot cannot be hand-edited
  into agreement -- it has to be regenerated from RegEngine's source.
* the ``regengine-contract`` job in ``.github/workflows/ci.yml`` checks
  RegEngine out next to this repo, re-extracts the table, and fails if the
  snapshot or REQUIRED_KDES differs from it.

So this file stays a fast offline test with no network and no second
repository, and the thing it compares against is now traceable to
upstream instead of asserted to be.

If RegEngine changes its required KDEs: regenerate the snapshot
(``python scripts/regengine_kde_contract.py snapshot --regengine-root
<checkout> --source-commit <sha>``), update ``app/cte_rules.py`` to match,
and bump INFLOW_CONTRACT_VERSION on both sides. A mismatch here means the
mock validates differently from live RegEngine, which resurrects the
"green demo, failing live post" drift this pin exists to catch.
"""

from __future__ import annotations

from app.cte_rules import REQUIRED_KDES
from app.schemas.domain import CTEType
from scripts.regengine_kde_contract import load_snapshot


# {cte_type_wire_value: (required KDE, ...)}, straight from the snapshot of
# RegEngine's REQUIRED_KDES_BY_CTE. Keyed by wire value rather than CTEType
# so a CTE type RegEngine adds and this repo has not modelled yet fails as
# a readable assertion below instead of a KeyError at import time.
REGENGINE_REQUIRED_KDES_BY_CTE: dict[str, tuple[str, ...]] = load_snapshot().table


def test_required_kdes_match_regengine_contract_exactly() -> None:
    running = {cte_type.value: tuple(kdes) for cte_type, kdes in REQUIRED_KDES.items()}

    assert running == REGENGINE_REQUIRED_KDES_BY_CTE


def test_every_cte_type_has_required_kdes() -> None:
    assert set(REQUIRED_KDES) == set(CTEType)

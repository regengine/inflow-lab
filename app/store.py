from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .schemas.domain import LineageEdge, LineageNode, StoredEventRecord


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


def _serialize_record(record: StoredEventRecord) -> str:
    """Render one record as the JSONL line that goes on disk.

    This is the single choke point every persisted-record write path funnels
    through (append in ``add_many``, rewrite in ``update_many``/
    ``replace_all``). Routing them all through the same function is what
    keeps the ``_scrub_secrets`` pass from drifting out of sync between an
    append and a rewrite of the same record.
    """
    return json.dumps(_scrub_secrets(record.model_dump(mode="json")))


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

    def _write_records(self, records_oldest_first: Iterable[StoredEventRecord]) -> None:
        """Rewrite ``persist_path`` atomically from a full oldest-first list.

        Shared by ``update_many``/``replace_all`` so a rewrite always goes
        through ``_serialize_record`` — the scrub can't be forgotten on one
        path and not the other. The write itself lands via a ``.tmp`` +
        ``replace`` swap, so ``persist_path`` on disk is always either the
        old file or the fully-written new one, never a half-written file,
        no matter where in the loop a write failure happens.

        Private, and assumes the caller already holds ``self._lock`` for the
        surrounding read-modify-write — it does not take the lock itself, so
        callers can invoke it without any reentrant-locking hazard. It says
        nothing about in-memory state: this only guarantees the file: each
        caller is responsible for its own ordering of ``_set_records``/
        ``_counter`` around the call.
        """
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.persist_path.with_suffix(f"{self.persist_path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in records_oldest_first:
                handle.write(_serialize_record(record) + "\n")
        tmp_path.replace(self.persist_path)

    def add_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        stored: list[StoredEventRecord] = []
        with self._lock:
            with self.persist_path.open("a", encoding="utf-8") as handle:
                for record in records:
                    next_sequence_no = self._counter + 1
                    record.sequence_no = next_sequence_no
                    handle.write(_serialize_record(record) + "\n")
                    # write() only stages bytes in a userspace buffer --
                    # flush() forces the OS-level write so a full disk (or
                    # any other write failure) raises here, synchronously,
                    # instead of being deferred to a later write's flush or
                    # to the handle's close, by which point we could have
                    # already committed more records to memory than ever
                    # reached disk.
                    handle.flush()
                    # Commit to memory only once the line is confirmed on
                    # disk. A write that fails partway through a batch (full
                    # disk, detached volume, permission change) must not
                    # leave a phantom record that recent()/stats() report
                    # until the next reload silently drops it again.
                    self._counter = next_sequence_no
                    self._records.appendleft(record)
                    stored.append(record)
        return stored

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

            # Persist before publishing, for the same reason as add_many: a
            # rewrite that fails here must leave the store matching the disk
            # rather than a half-applied update that recent()/stats() report
            # until the next reload silently drops it.
            persisted_records = sorted(updated_records, key=lambda record: record.sequence_no)
            self._write_records(persisted_records)
            self._set_records(updated_records)
            self._counter = max((record.sequence_no for record in persisted_records), default=0)

        return [record for record in updated_records if record.record_id in replacements]

    def replace_all(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        persisted_records = sorted(list(records), key=lambda record: record.sequence_no)
        with self._lock:
            self._write_records(persisted_records)
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
        record_id_filter = set(record_ids or [])
        records = self._all_records()
        failed_records = [
            record
            for record in records
            if record.delivery_status == "failed"
            and (not record_id_filter or record.record_id in record_id_filter)
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

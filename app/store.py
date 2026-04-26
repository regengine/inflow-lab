from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .models import LineageEdge, LineageNode, StoredEventRecord


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

    def _load_from_disk(self) -> None:
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        records = self.read_persisted_records(str(self.persist_path))
        loaded_records: deque[StoredEventRecord] = deque(records, maxlen=self.max_records)
        counter = max((record.sequence_no for record in records), default=0)

        self._records = deque(reversed(loaded_records), maxlen=self.max_records)
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

    def add_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        stored: list[StoredEventRecord] = []
        with self._lock:
            with self.persist_path.open("a", encoding="utf-8") as handle:
                for record in records:
                    self._counter += 1
                    record.sequence_no = self._counter
                    self._records.appendleft(record)
                    handle.write(json.dumps(record.model_dump(mode="json")) + "\n")
                    stored.append(record)
        return stored

    def recent(self, limit: int = 100) -> list[StoredEventRecord]:
        with self._lock:
            return list(self._records)[:limit]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)
        cte_counter = Counter(record.event.cte_type.value for record in records)
        status_counter = Counter(record.delivery_status for record in records)
        destination_counter = Counter(record.destination_mode.value for record in records)
        unique_lots = {record.event.traceability_lot_code for record in records}
        return {
            "total_records": len(records),
            "unique_lots": len(unique_lots),
            "by_cte_type": dict(cte_counter),
            "by_delivery_status": dict(status_counter),
            "by_destination": dict(destination_counter),
            "persist_path": str(self.persist_path),
        }

    def lineage(self, traceability_lot_code: str) -> list[StoredEventRecord]:
        with self._lock:
            records = list(self._records)
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
        with self._lock:
            records = list(self._records)
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

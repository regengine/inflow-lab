from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .models import DestinationMode, StoredEventRecord


MASKED_SECRET = "***MASKED***"
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


class EventStore:
    def __init__(self, persist_path: str = "data/events.jsonl", max_records: int = 5000) -> None:
        self.persist_path = Path(persist_path)
        self.max_records = max_records
        self._records: deque[StoredEventRecord] = deque(maxlen=max_records)
        self._counter = 0
        self._lock = RLock()
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)

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
                    handle.write(json.dumps(_scrub_secrets(record.model_dump(mode="json"))) + "\n")
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
        matched = [
            record
            for record in records
            if record.event.traceability_lot_code == traceability_lot_code
            or traceability_lot_code in record.parent_lot_codes
            or traceability_lot_code in record.event.kdes.get("input_traceability_lot_codes", [])
            or record.event.kdes.get("source_traceability_lot_code") == traceability_lot_code
        ]
        matched.sort(key=lambda record: record.event.timestamp)
        return matched

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

from __future__ import annotations

import json
import logging
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, NamedTuple

from .schemas.domain import LineageEdge, LineageNode, StoredEventRecord


# Same name-keyed singleton logger main.py configures. A write that never
# reaches disk is the one failure this store cannot recover from, so it must
# not be silent even though the exception propagates (#182).
logger = logging.getLogger("inflow_lab")


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


class CorruptRecordLine(NamedTuple):
    """One JSONL line ``read_persisted_records`` could not parse and skipped.

    Populated on ``EventStore.last_read_corrupt_lines`` after every
    ``read_persisted_records()`` call (#93) so a caller holding the store
    can tell a read that quietly dropped bad lines apart from one that
    returned the tenant's complete history -- see
    ``EventStore.last_read_was_complete``. Never carries the line's own
    content: it failed to parse *as* a record, so it could still hold a
    secret-named KDE the model never got a chance to scrub.
    """

    path: str
    line_number: int
    error: str


class EventStore:
    def __init__(self, persist_path: str = "data/events.jsonl", max_records: int = 5000) -> None:
        self.persist_path = Path(persist_path)
        self.max_records = max_records
        self._records: deque[StoredEventRecord] = deque(maxlen=max_records)
        self._counter = 0
        # Freshly overwritten by every read_persisted_records() call (#93);
        # see that method and last_read_was_complete for why this exists.
        # Initialized empty here so the attribute always exists even before
        # the _load_from_disk() call a few lines down performs the first
        # real read.
        self.last_read_corrupt_lines: list[CorruptRecordLine] = []
        # threading.RLock, not asyncio.Lock (#136). Two different kinds of
        # caller take it: EventStore.__init__/_load_from_disk and
        # MockRegEngineService.__init__ (mock_service.py, via
        # read_persisted_records) call in from plain synchronous code with
        # no event loop running at all, while app.controller offloads the
        # write paths (add_many/update_many/replace_all/configure) and the
        # replay read (read_persisted_records) onto worker threads via
        # asyncio.to_thread so a large write or replay can't stall the
        # single event loop (see the locking comment in
        # SimulationController, app/controller.py).
        #
        # That split only works because every public method on this class
        # is a plain `def`, never `async def`: each one acquires and fully
        # releases this lock within its own synchronous call frame, with no
        # `await` anywhere inside the locked section for it to suspend on.
        # asyncio.to_thread runs one such call start-to-finish on a single
        # worker thread, which preserves both guarantees this lock has to
        # provide:
        #   - cross-call mutual exclusion, because RLock serializes *any*
        #     threads that ask for it, not just the event loop's own thread;
        #   - same-call reentrancy (configure -> _load_from_disk ->
        #     read_persisted_records; update_many -> _all_records ->
        #     read_persisted_records), because RLock is only reentrant for
        #     the thread that already holds it, and that nested chain never
        #     leaves the one worker thread running it.
        # Do NOT turn one of these methods into `async def` while it still
        # holds this lock across an `await` -- that would let the event
        # loop thread block waiting on a worker thread for the lock while
        # that worker is itself waiting on the event loop to resume it: a
        # deadlock. If a method here ever needs to suspend mid-critical-
        # section, that is a sign it no longer belongs behind this lock as
        # written, not a reason to relax the "no await while held" rule.
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
        """Read the full persisted log from disk, synchronously.

        Stays a plain synchronous method deliberately (#136): it is called
        from EventStore.__init__/_load_from_disk and from
        MockRegEngineService.__init__ (mock_service.py), both of which run
        before/without any event loop, so an ``async def`` here would break
        construction. Async callers with a loop running (app.controller's
        ``replay``) instead offload a whole call to a worker thread via
        ``asyncio.to_thread`` -- see the lock comment in ``__init__`` above
        for why that is safe.

        A line that fails to parse -- a future StoredEventRecord/CTEType
        schema change, a write torn by a crash mid-append, a hand-edited
        file -- is skipped and logged rather than aborting the whole read
        (#93): one bad line must not make a tenant's entire history
        unreadable, and since __init__/_load_from_disk calls this too, it
        must not stop the app from starting on a corrupt store either.

        The return type deliberately stays a plain list so callers that
        already unpack it directly keep working unchanged (mock_service.py's
        ``_resume_chain_hash`` does ``sorted(store.read_persisted_records(),
        ...)``; controller.py's ``_store_read_persisted_records`` awaits this
        through ``asyncio.to_thread`` and hands the list straight to its own
        caller). A skipped line is instead recorded on
        ``self.last_read_corrupt_lines`` -- see ``last_read_was_complete``
        for how a caller tells a read that dropped lines apart from one that
        returned the complete history.
        """
        path = Path(persist_path) if persist_path else self.persist_path
        records: list[StoredEventRecord] = []
        corrupt_lines: list[CorruptRecordLine] = []

        with self._lock:
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            records.append(StoredEventRecord.model_validate_json(line))
                        except ValueError as exc:
                            # Skip and log rather than abort (#93). The raw
                            # line itself is never logged: it failed to
                            # parse *as* a record, so it could still carry a
                            # secret-named KDE the model never got a chance
                            # to scrub.
                            logger.error(
                                "skipping unreadable event record at %s:%d (%s)",
                                path,
                                line_number,
                                exc,
                            )
                            corrupt_lines.append(
                                CorruptRecordLine(path=str(path), line_number=line_number, error=str(exc))
                            )
            # Overwritten every call, including empty, so this always
            # reflects *this* read rather than stale state left by a
            # previous one -- see last_read_was_complete.
            self.last_read_corrupt_lines = corrupt_lines
        return records

    @property
    def last_read_was_complete(self) -> bool:
        """False if the most recent ``read_persisted_records()`` skipped a line.

        A short or empty read result can mean two very different things:
        "this genuinely is the tenant's whole history" or "some of it was
        unreadable and got skipped" (#93). Callers that need to tell those
        apart -- rather than inferring completeness from record count alone
        -- should check this immediately after the read. It reflects the
        single most recent ``read_persisted_records()`` call on this store,
        including nested/reentrant ones made while already holding this
        store's lock (``_load_from_disk``, and ``update_many`` via
        ``_all_records``).
        """
        return not self.last_read_corrupt_lines

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._counter = 0
            # The file backing last_read_corrupt_lines is about to be
            # deleted; carrying its stale diagnostics forward would make a
            # freshly reset (empty, healthy) store still report a past
            # corruption that reset() just removed.
            self.last_read_corrupt_lines = []
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
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                for record in records_oldest_first:
                    handle.write(_serialize_record(record) + "\n")
            tmp_path.replace(self.persist_path)
        except OSError:
            # Re-raised, never swallowed: callers order their in-memory
            # commit around this and depend on the failure propagating.
            logger.exception("event store rewrite failed for %s", self.persist_path)
            raise

    def add_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        stored: list[StoredEventRecord] = []
        try:
            return self._append_many(records, stored)
        except OSError:
            # Re-raised: the caller must see the failure, and the in-memory
            # rollback below the append depends on it propagating.
            logger.exception("event store append failed for %s", self.persist_path)
            raise

    def _append_many(
        self, records: Iterable[StoredEventRecord], stored: list[StoredEventRecord]
    ) -> list[StoredEventRecord]:
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
            # #93: lets a caller of the already-wired /api/simulate/status
            # and /api/health (both flow stats() into their response) tell
            # a read that skipped unreadable lines apart from one that
            # returned the tenant's complete history -- without changing
            # the shape of anything that already unpacks stats()'s return
            # value (it stays a plain dict; this only adds a key).
            "store_integrity": {
                "complete": self.last_read_was_complete,
                "corrupt_lines_skipped": len(self.last_read_corrupt_lines),
            },
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

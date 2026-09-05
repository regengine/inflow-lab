from __future__ import annotations

import json
import logging
import os
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, NamedTuple

from .schemas.domain import LineageEdge, LineageNode, StoredEventRecord


# Same name-keyed singleton logger main.py configures. A write that never
# reaches disk is the one failure this store cannot recover from, so it must
# not be silent even though the exception propagates (#182).
logger = logging.getLogger("inflow_lab")


# Retention bound for the live on-disk log (#217). Every history read --
# stats(), lineage(), all_between(), both exports, and /api/simulate/status
# on the console's poll timer -- parses the WHOLE live log through Pydantic
# via _all_records(), so a log that only ever grows makes each of those
# slower for the life of the deployment. Once the log holds more than this
# many records (plus a slack, see EventStore._rotate_history_if_needed) the
# oldest are moved to an archive beside it and the live log is rewritten to
# the newest max_history. Override with REGENGINE_STORE_MAX_HISTORY.
MAX_HISTORY_ENV = "REGENGINE_STORE_MAX_HISTORY"
DEFAULT_MAX_HISTORY = 50_000


def default_max_history() -> int:
    """Resolve the live-log retention bound from the environment (#217).

    Same degrade-to-default convention as tenancy._parse_tenant_cap: a
    malformed or non-positive value is logged and falls back to
    DEFAULT_MAX_HISTORY rather than raising, because this runs at import
    time through app/tenancy.py's default store and a typo in one env var
    must not stop the app from starting.
    """
    raw = os.getenv(MAX_HISTORY_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_HISTORY
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        logger.warning(
            "%s=%r is not a valid positive integer; falling back to default (%d)",
            MAX_HISTORY_ENV,
            raw,
            DEFAULT_MAX_HISTORY,
        )
        return DEFAULT_MAX_HISTORY


class RetiredStoreError(ValueError):
    """Raised when a write is attempted through a deleted tenant's store (#175).

    Subclasses ValueError so main.py's app-wide handler turns it into a clean
    4xx rather than an unhandled 500.
    """


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


def _fsync_directory(directory: Path) -> None:
    """Flush ``directory``'s entries to stable storage, best-effort (#217).

    ``os.replace`` swaps a directory *entry*, and that entry lives in the
    directory's own metadata: fsyncing the new file's bytes says nothing
    about whether the rename itself survives a power loss. Without this a
    rewrite could be acknowledged and then, after a hard restart, the OLD
    file is still what the directory points at.

    Best-effort in the same way ``_fsync_appended`` is: a filesystem that
    refuses to open or fsync a directory (some FUSE, overlay and network
    mounts) must not turn an otherwise-successful rewrite into a failure
    the caller then rolls its in-memory state back for.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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


def _describe_parse_failure(exc: Exception) -> str:
    """Describe why a line failed to parse, WITHOUT quoting the line.

    Both this module's log line and ``CorruptRecordLine.error`` promise not
    to carry the offending content: a line that failed to parse *as* a
    record never went through ``_scrub_secrets``, so it can still hold a
    plaintext ``api_key``. ``str(ValidationError)`` breaks that promise --
    pydantic renders ``input_value=...`` into its message, so a corrupt line
    carrying a live key put that key straight into the log.

    ``errors(include_input=False)`` drops exactly that field and keeps what
    is actually diagnostic: the failure type, the field location, and the
    message. ``include_url=False`` trims the docs link, which adds nothing
    to a log. A non-pydantic ValueError (nothing raises one here today, but
    the caller catches the base class) has no structured form, so it falls
    back to ``str`` -- those messages are ours, not echoes of the input.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    try:
        rendered = errors(include_url=False, include_input=False)
    except TypeError:  # pragma: no cover - only on a pydantic without the kwargs
        return f"{type(exc).__name__}: unparseable record line"
    return "; ".join(
        f"{'.'.join(str(part) for part in item.get('loc', ())) or '<record>'}: {item.get('msg', '')}"
        for item in rendered
    )


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
    def __init__(
        self,
        persist_path: str = "data/events.jsonl",
        max_records: int = 5000,
        max_history: int | None = None,
    ) -> None:
        self.persist_path = Path(persist_path)
        self.max_records = max_records
        self._records: deque[StoredEventRecord] = deque(maxlen=max_records)
        self._counter = 0
        # Live-log retention bound (#217); see _rotate_history_if_needed. An
        # explicit argument wins so a test can pick a small bound without
        # touching the environment; otherwise REGENGINE_STORE_MAX_HISTORY,
        # else DEFAULT_MAX_HISTORY. Refused rather than clamped when it is
        # explicitly non-positive: `records[-0:]` is the whole list, so a
        # zero here would silently disable the trim instead of tightening it.
        if max_history is not None and max_history < 1:
            raise ValueError("max_history must be a positive integer")
        self.max_history = max_history if max_history is not None else default_max_history()
        # Records currently in the live log, maintained by every write path,
        # so deciding WHETHER to rotate is an integer compare on the append
        # hot path rather than a re-parse of the whole file per batch. Only
        # the trigger relies on it: the rotation itself re-reads the log and
        # trims from what is actually there, so an estimate that has drifted
        # (another writer, a skipped corrupt line) can only mis-time a
        # rotation, never archive the wrong records.
        self._persisted_count = 0
        # Freshly overwritten by every read_persisted_records() call (#93);
        # see that method and last_read_was_complete for why this exists.
        # Initialized empty here so the attribute always exists even before
        # the _load_from_disk() call a few lines down performs the first
        # real read.
        self.last_read_corrupt_lines: list[CorruptRecordLine] = []
        # Parsed-log cache (#65). `_all_records()` backs stats(), lineage(),
        # all_between() and status(), and reparsed the entire JSONL through
        # Pydantic on every one of them -- once per /api/simulate/status poll,
        # which the console issues on a timer. Keyed on the file's
        # (mtime_ns, size) rather than on this process's own writes, so a
        # change made by any path -- another worker, an operator editing the
        # file, a volume restore -- invalidates it too. Both parts matter:
        # size alone misses an in-place edit, and mtime alone can miss a write
        # that lands inside the same filesystem timestamp tick.
        self._records_cache: list[StoredEventRecord] | None = None
        self._records_cache_key: tuple[int, int] | None = None
        # Set by retire() when this store's tenant is deleted; see retire().
        # Declared here so the attribute always exists, including for the
        # _load_from_disk() call below, which clears it.
        self.retired = False
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
        # configure() routes here to repoint the store at a different file.
        # That is a new backing store, not the retired one, so the retirement
        # does not carry over -- otherwise a tenant id reused after a delete
        # would inherit a permanently unwritable store.
        self.retired = False
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._discard_stale_rewrite_tmp()
        records = self.read_persisted_records(str(self.persist_path))
        counter = max((record.sequence_no for record in records), default=0)

        self._set_records(records)
        self._counter = counter
        self._persisted_count = len(records)

    def _rewrite_tmp_path(self) -> Path:
        """Where ``_write_records`` builds a rewrite before swapping it in.

        One definition, so the cleanup in ``_discard_stale_rewrite_tmp`` can
        never look for a different name than the writer actually uses.
        """
        return self.persist_path.with_suffix(f"{self.persist_path.suffix}.tmp")

    def _discard_stale_rewrite_tmp(self) -> None:
        """Remove a ``.tmp`` a rewrite never got to swap in (#217).

        ``_write_records`` builds the new log at ``<persist>.tmp`` and
        ``replace``s it over the live one. A process SIGKILLed -- or a
        machine powered off -- between those two steps leaves the half-built
        file behind. It is never read as data (the live log is still the
        old, complete file), but nothing cleared it either, so it sat beside
        the log until the next rewrite happened to reuse the name.

        Best-effort: this runs at import time through app/tenancy.py's
        default store, and a stray file that cannot be removed must not stop
        the app from starting (the same rule #93 set for a corrupt log).
        """
        tmp_path = self._rewrite_tmp_path()
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove stale event store temp file %s", tmp_path)

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
                # Opened in BINARY mode, deliberately (#93). A text-mode
                # handle decodes as it iterates, so a run of invalid UTF-8
                # bytes anywhere in the file raises UnicodeDecodeError out
                # of the `for` statement itself -- outside the try below,
                # and therefore past every skip-and-log guard here. That is
                # the same whole-read abort this method exists to prevent,
                # just triggered by byte corruption instead of a syntax
                # error: torn writes, a truncated volume restore, or a file
                # written by something that was not this app. Because
                # app/tenancy.py builds the default store at import time,
                # that abort also stopped the app starting at all on a
                # corrupt volume.
                #
                # Reading bytes and letting model_validate_json do the
                # decoding moves that failure inside the try, where it
                # arrives as a pydantic ValidationError (a ValueError) and
                # is skipped and logged exactly like any other unparseable
                # line. Iterating a binary handle still splits on b"\n",
                # which is precisely what _serialize_record writes, so
                # line numbering is unchanged.
                with path.open("rb") as handle:
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
                            # to scrub. Rendered by _describe_parse_failure
                            # rather than str(exc), because pydantic puts
                            # the rejected input INTO its message -- which
                            # made that promise false for as long as it has
                            # been written here.
                            detail = _describe_parse_failure(exc)
                            logger.error(
                                "skipping unreadable event record at %s:%d (%s)",
                                path,
                                line_number,
                                detail,
                            )
                            corrupt_lines.append(
                                CorruptRecordLine(path=str(path), line_number=line_number, error=detail)
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
            self._persisted_count = 0
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

        After the swap the parent directory is fsynced as well (#217): the
        file's bytes being on disk does not make the *rename* durable -- see
        ``_fsync_directory`` for why that is best-effort.
        """
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._rewrite_tmp_path()
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                for record in records_oldest_first:
                    handle.write(_serialize_record(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(self.persist_path)
        except Exception:
            # Re-raised, never swallowed: callers order their in-memory
            # commit around this and depend on the failure propagating.
            # ``Exception`` rather than ``OSError`` (#217): a serialization
            # failure out of _serialize_record is a ValueError, not an
            # OSError, and it used to propagate correctly while leaving the
            # half-built .tmp orphaned beside the live log.
            logger.exception("event store rewrite failed for %s", self.persist_path)
            tmp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(self.persist_path.parent)

    def retire(self) -> None:
        """Mark this store dead because its tenant is being deleted (#175).

        A request that resolved its controller *before* the delete started
        still holds that controller object, and every write path here does
        ``mkdir(parents=True)`` on its own -- so a write through the popped
        controller recreated the tenant directory after the rmtree, and
        ``known_tenant_ids()`` then listed the tenant as present again. That
        controller was a genuine zombie: the next request for the tenant
        minted a second EventStore over the same file with an independent
        ``_counter``, so two live objects handed out the same sequence
        numbers.

        Retiring makes those in-flight writes fail loudly instead of
        silently resurrecting the tenant. Reads are deliberately still
        allowed: a request already holding this object may legitimately
        finish rendering what it had, and a read cannot recreate anything.
        """
        with self._lock:
            self.retired = True

    def _refuse_if_retired(self) -> None:
        if self.retired:
            raise RetiredStoreError(
                "This tenant has been deleted; its event store is no longer writable. "
                "Retry the request to work against a freshly created tenant."
            )

    def add_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        self._refuse_if_retired()
        stored: list[StoredEventRecord] = []
        try:
            return self._append_many(records, stored)
        except OSError:
            # Re-raised: the caller must see the failure, and the in-memory
            # rollback below the append depends on it propagating.
            logger.exception("EventStore append failed: path=%s", self.persist_path)
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
                    self._persisted_count += 1
                    self._records.appendleft(record)
                    stored.append(record)
                if stored:
                    self._fsync_appended(handle)
            # Only once the batch is durably appended and the append handle
            # is closed: rotation may replace the file this handle points at.
            if stored:
                self._rotate_history_if_needed()
        return stored

    def _rotate_history_if_needed(self) -> None:
        """Bound the live log to the newest ``max_history`` records (#217).

        Called under ``self._lock`` after a batch has been durably appended.
        Once the log exceeds ``max_history`` by a slack of
        ``max(1, max_history // 10)`` records, the oldest overflow is
        appended to an archive beside the log (``<persist>.1``, fsynced) and
        the live log is rewritten to the newest ``max_history`` records
        through ``_write_records`` -- the same atomic swap + fsync path the
        retry and scenario-load rewrites already use. The slack is what
        keeps the append path O(1) amortized: a rewrite costs
        O(max_history), so it must happen once per slack-many appended
        records, not once per batch that lands at the bound.

        Archive first, then trim, deliberately: a failure between the two
        steps duplicates the overflow in the archive on the next successful
        rotation, whereas the other order would lose those records outright.

        Best-effort: the append that triggered this has already been
        acknowledged to its caller and is on disk, so an OSError here is
        logged and swallowed -- a rotation problem must not turn a
        successful ingest into a reported failure. Sequence numbers are
        untouched: ``_counter`` keeps its high-water mark, and because the
        newest record is always retained, a fresh store loading the trimmed
        log recomputes that same mark, so numbering never restarts.
        """
        slack = max(1, self.max_history // 10)
        if self._persisted_count <= self.max_history + slack:
            return
        try:
            records = self._all_records()
            if len(records) <= self.max_history:
                # The estimate ran ahead of the file (see __init__); nothing
                # to trim after all, so just correct it.
                self._persisted_count = len(records)
                return
            overflow = records[: -self.max_history]
            retained = records[-self.max_history :]
            self._archive_records(overflow)
            self._write_records(retained)
            self._set_records(retained)
            self._persisted_count = len(retained)
            logger.info(
                "event store rotated: %d oldest record(s) archived to %s; %s trimmed to the newest %d",
                len(overflow),
                self._archive_path(),
                self.persist_path,
                len(retained),
            )
        except OSError:
            logger.exception(
                "event store rotation failed for %s; the append that triggered it succeeded",
                self.persist_path,
            )

    def _archive_path(self) -> Path:
        return self.persist_path.with_name(f"{self.persist_path.name}.1")

    def _archive_records(self, records_oldest_first: list[StoredEventRecord]) -> None:
        """Append rotated-out records to ``<persist>.1``, durably (#217).

        A plain append + fsync, like ``_append_many``'s own path: the archive
        is written once per rotation and never read back by this class, so
        it needs no tmp + swap. Still routed through ``_serialize_record``
        like every other persisted-record write, so the secret scrub (#90)
        cannot be bypassed here either -- it is idempotent on records that
        were already scrubbed when they were first appended.
        """
        with self._archive_path().open("a", encoding="utf-8") as handle:
            for record in records_oldest_first:
                handle.write(_serialize_record(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _fsync_appended(self, handle: Any) -> None:
        """Force this batch's appended bytes to stable storage (#93).

        ``flush()`` in the loop above only pushes the bytes out of Python's
        buffer into the OS page cache. That is enough to make a write error
        surface synchronously and enough to survive the process dying, but
        not enough to survive the machine losing power -- so an
        acknowledged append could still be missing after a hard restart.
        ``update_many``/``replace_all`` get their durability from the tmp +
        ``os.replace`` swap in ``_write_records``; the append path had no
        equivalent, which is the durability half of #93.

        A tmp + rename would give the same guarantee here only by
        rewriting the entire log on every append -- turning an O(1) append
        into O(total records), on the hot path every step() takes. #93
        names ``fsync`` as the alternative for exactly this reason.

        One fsync per *batch*, not per record: the per-record ``flush()``
        already supplies the error detection and the write-then-commit
        ordering the loop depends on, and an fsync per record would make a
        multi-thousand-row CSV import pay a disk round-trip per row.

        Skipped for anything that is not a regular file: the persist path
        can legitimately point at a character device (the durability tests
        use /dev/full to force ENOSPC), which has no durable state to
        flush. Same constraint ``_truncate_torn_tail`` and
        ``_resync_counter_from_disk`` already carry, for the same reason.
        """
        if not self.persist_path.is_file():
            return
        os.fsync(handle.fileno())

    def update_many(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        self._refuse_if_retired()
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
            self._persisted_count = len(persisted_records)

        return [record for record in updated_records if record.record_id in replacements]

    def replace_all(self, records: Iterable[StoredEventRecord]) -> list[StoredEventRecord]:
        self._refuse_if_retired()
        persisted_records = sorted(list(records), key=lambda record: record.sequence_no)
        with self._lock:
            self._write_records(persisted_records)
            self._set_records(persisted_records)
            self._counter = max((record.sequence_no for record in persisted_records), default=0)
            self._persisted_count = len(persisted_records)
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

    def _cache_key(self) -> tuple[int, int] | None:
        """(mtime_ns, size) of the log, or None when it cannot be stat'd.

        None means "do not cache": a missing or unreadable path has no
        identity to key on, and caching under a placeholder would serve a
        stale answer once the file appeared.
        """
        try:
            stat = self.persist_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _all_records(self) -> list[StoredEventRecord]:
        with self._lock:
            key = self._cache_key()
            if key is not None and key == self._records_cache_key and self._records_cache is not None:
                return self._records_cache

            records = self.read_persisted_records()
            if records:
                ordered = sorted(records, key=lambda record: record.sequence_no)
            else:
                ordered = sorted(self._records, key=lambda record: record.sequence_no)

            # Re-read the key AFTER parsing: if the file changed while this
            # read was in flight, the pre-read key would cache content that
            # never matched it, and the next call would serve that stale list
            # believing it current.
            if key is not None and key == self._cache_key():
                self._records_cache = ordered
                self._records_cache_key = key
            else:
                self._records_cache = None
                self._records_cache_key = None
            return ordered

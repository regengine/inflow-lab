"""The suite's canonical "recent, valid" event timestamp (#102, #209).

RegEngine rejects any event timestamped further in the past than
``WEBHOOK_MAX_EVENT_AGE_DAYS`` (90) with "replay window exceeded", and the
mock now models that floor by default (``MAX_EVENT_AGE_DAYS`` in
``app/mock_service.py``). Every test that posts an event and expects it to
be *accepted* therefore has to date that event inside the window.

A hardcoded literal cannot do that. It is inside the window on the day it
is typed and outside it 90 days later, so the suite would rot into a wall
of red on a date nobody chose -- the same silent decay that made the
shipped demo fixtures reject against live ingest before #199 rebased them
onto the current date. Half of #102 was exactly this: a fixed
``2026-02-05``-style timestamp used as the canonical valid event across a
dozen-plus modules, which is what kept the age window off by default long
after the fixtures had stopped being the obstacle.

So the canonical event time is computed relative to now, once, here, and
imported. Two rules:

* Import from this module for any event that is *meant to be accepted* --
  the timestamp is incidental to what those tests are actually about.
* Keep a hardcoded literal where the fixed value **is** the subject of the
  test: golden export snapshots, timezone-conversion arithmetic,
  byte-for-byte store durability, and the age-window tests themselves,
  which drive an injected clock rather than the wall clock and so are
  already immune to real time passing.

Resolved once, at import, rather than per call. Within a run every caller
then agrees on a single instant, so a test that builds two "identical"
events in different helpers still gets a genuine in-batch duplicate, and
``CANONICAL_EVENT_DATE`` cannot disagree with ``CANONICAL_EVENT_TIME``
across a midnight boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# How far before "now" the canonical event sits. Matches the engine's own
# anchor (``LegitFlowEngine.__init__``: ``datetime.now(UTC) -
# timedelta(hours=12)``), so a hand-built test event and a generated one
# are the same age and clear the same checks. It also leaves 12h of slack
# for helpers that add a few minutes per record without any of them
# crossing into the future, where the 24h ceiling would reject them
# instead.
CANONICAL_EVENT_AGE = timedelta(hours=12)

#: The canonical valid-event timestamp: recent, in the past, never stale.
CANONICAL_EVENT_TIME: datetime = (
    datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - CANONICAL_EVENT_AGE
)

#: ``CANONICAL_EVENT_TIME`` as the ``Z``-suffixed ISO-8601 string that JSON
#: request bodies and CSV ``timestamp`` columns carry on the wire.
CANONICAL_EVENT_TIMESTAMP: str = CANONICAL_EVENT_TIME.isoformat().replace("+00:00", "Z")

#: The bare ``YYYY-MM-DD`` calendar date of ``CANONICAL_EVENT_TIME``, for
#: the date-shaped KDEs (``harvest_date``, ``pack_date``, ``receive_date``
#: ...) that have to describe the same day as the event they hang off.
CANONICAL_EVENT_DATE: str = CANONICAL_EVENT_TIME.date().isoformat()


def canonical_event_time(offset: timedelta = timedelta()) -> datetime:
    """``CANONICAL_EVENT_TIME`` shifted by ``offset``.

    For lineage fixtures that need events ordered relative to each other.
    Keep ``offset`` inside +/- 12h so the result stays both inside the
    90-day floor and below the 24h future ceiling.
    """
    return CANONICAL_EVENT_TIME + offset


def canonical_event_timestamp(offset: timedelta = timedelta()) -> str:
    """``canonical_event_time(offset)`` as a ``Z``-suffixed ISO-8601 string."""
    return canonical_event_time(offset).isoformat().replace("+00:00", "Z")


def canonical_event_date(offset: timedelta = timedelta()) -> str:
    """``canonical_event_time(offset)`` as a bare ``YYYY-MM-DD`` date."""
    return canonical_event_time(offset).date().isoformat()

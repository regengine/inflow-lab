"""Canonical event timestamps for tests that deliver through the mock.

The mock enforces RegEngine's 90-day event-age replay window by default
(#209/#102), so a test that hands it an event must supply one inside that
window. A fixed literal cannot: `datetime(2026, 2, 5, ...)` was a perfectly
good canonical timestamp when it was written and is silently out of window
some months later, which turns every mock-ingest test into a time bomb that
goes off long after the change that "caused" it.

`recent_event_timestamp()` is that canonical timestamp, computed relative to
now. It keeps the 08:00 UTC time-of-day the literals used, so any test
asserting on a rendered time still reads naturally, and it sits far enough
inside the window that a suite running slowly, or across a midnight boundary,
cannot drift out of it.

Tests that are ABOUT the window -- a deliberately stale event, a boundary
probe -- should keep building their own timestamps relative to
`mock_service.MAX_EVENT_AGE_DAYS`, not use this. This is only for the
"an ordinary valid event" case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


# Far enough inside the 90-day window that a slow suite, a timezone edge or a
# test that walks its events forward by a few days stays comfortably within it.
RECENT_EVENT_AGE_DAYS = 3


def recent_event_timestamp(*, days_ago: int = RECENT_EVENT_AGE_DAYS) -> datetime:
    """A valid, in-window event timestamp at 08:00 UTC."""
    day = (datetime.now(UTC) - timedelta(days=days_ago)).date()
    return datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)

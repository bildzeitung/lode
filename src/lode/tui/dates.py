"""Adaptive human-readable date formatting for the browse screen (lode-1gr.8).

``notes.created`` / ``versions.created`` are full ISO-8601 UTC timestamps (the shared
``%Y-%m-%dT%H:%M:%S.%fZ`` stamp :mod:`lode.worker`/:mod:`lode.versions` write) -- exact
and sortable, but a lot of characters to spend on every row of
:class:`~lode.tui.screens.browse.BrowseScreen`'s Date column (and its version-history
sibling) when most of that precision is noise to a human scanning the list. This
module's :func:`format_adaptive_date` buckets a timestamp against "now" into the
shortest representation that still reads unambiguously:

* **today** -- just the time (``14:30``)
* **this week** (1-6 days ago) -- weekday + time (``Mon 09:12``)
* **this year** (older, same year) -- month + day (``Jul 3``)
* **older / a prior year** -- the plain ISO date (``2025-11-20``)

A standalone function (not a method on :class:`~lode.notes_read.NoteRow` /
:class:`~lode.notes_read.VersionRow`) so it stays unit-testable against a
fixed ``now`` without touching a database -- :mod:`lode.notes_read` itself
keeps returning the raw ISO string unchanged (the CLI's ``lode notes``/``lode
show`` still want their own, longer-but-still-shortened form, per
``lode.cli._short_date``'s docstring, which explicitly defers this adaptive
format to Browse); only the two TUI render sites
(:meth:`~lode.tui.screens.browse.BrowseScreen._reload_rows`,
:meth:`~lode.tui.screens.version_history.VersionHistoryScreen.on_mount`) call
this to shorten what they display.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lode.timestamps import parse_stamp

#: How many days back from "now" still count as "this week" (weekday + time)
#: rather than falling through to the "this year" (month + day) bucket -- a
#: rolling 6-day window rather than a calendar-week boundary, so it needs no
#: locale/week-start decision.
_WEEK_WINDOW_DAYS = 7


def format_adaptive_date(created: str, *, now: datetime | None = None) -> str:
    """Shorten an ISO-8601 UTC ``created`` timestamp into an adaptive human form.

    ``created`` is stored and parsed as UTC, but displayed in the system
    local timezone (lode-olmi.5): both the parsed timestamp and the
    reference "now" are converted with ``.astimezone()`` (no argument --
    system local) before bucketing or formatting, so a note captured near
    midnight UTC lands in the calendar day/bucket a human actually
    experienced it in, not the UTC one.

    ``now`` defaults to the real current UTC time; tests pass a fixed instant
    so each bucket is deterministic. Buckets compare calendar dates (in local
    time), not elapsed hours, so an 11pm-yesterday note reads as "this week"
    rather than "today" even though it's under 24h old.
    """
    dt = parse_stamp(created).astimezone()
    reference = (now if now is not None else datetime.now(UTC)).astimezone()
    delta_days = (reference.date() - dt.date()).days
    if delta_days == 0:
        return dt.strftime("%H:%M")
    if 0 < delta_days < _WEEK_WINDOW_DAYS:
        return dt.strftime("%a %H:%M")
    if dt.year == reference.year:
        return f"{dt:%b} {dt.day}"
    return dt.strftime("%Y-%m-%d")

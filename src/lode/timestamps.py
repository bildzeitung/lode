"""Shared UTC-stamp parsing helper (lode-aswc).

``notes.created`` / ``versions.created`` are written as a single stamp
format -- ``%Y-%m-%dT%H:%M:%S.%fZ`` (:mod:`lode.worker`/:mod:`lode.versions`)
-- and every human-facing surface that displays a note's timestamp needs to
parse that stamp back into an aware-UTC :class:`~datetime.datetime` before
converting to local time or bucketing it. Three call sites
(:func:`lode.cli._short_date`, :func:`lode.tui.dates.format_adaptive_date`,
:func:`lode.tui.services.related.humanize_age`) each carried their own verbatim
``datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=...)``
copy, so a stamp-format change would have needed the same edit three times.

This module lives outside both :mod:`lode.cli` and the :mod:`lode.tui`
package so every call site can share one helper without a cyclic or
backwards import -- ``lode.cli`` must not import from ``tui``, so neither
``lode.cli`` nor ``lode.tui.dates`` is a valid home for the shared parse.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: The one stamp format ``notes.created`` / ``versions.created`` are written
#: in (``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` in ``schema.sql``).
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def parse_stamp(created: str) -> datetime:
    """Parse the shared ``%Y-%m-%dT%H:%M:%S.%fZ`` UTC stamp to an aware datetime.

    ``created`` is a ``notes.created`` / ``versions.created`` value. The
    result is always UTC-aware; callers that need to display it convert with
    ``.astimezone()`` themselves -- this helper only owns the parse.
    """
    return datetime.strptime(created, STAMP_FORMAT).replace(tzinfo=UTC)

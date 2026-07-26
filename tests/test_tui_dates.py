"""Tests for the browse screen's adaptive date formatter (lode-1gr.8).

Fixed ``now`` instants make every bucket deterministic: no real-clock flake,
no DB. One test per bucket (today, this week, this year, older/prior year),
plus the boundary cases the bucketing logic hinges on.

``format_adaptive_date`` converts to system local time before bucketing and
formatting (lode-olmi.5), so these tests pin the process's local timezone
via ``_local_tz`` rather than relying on whatever timezone happens to be
configured on the machine running them. The original bucket/boundary tests
below pin ``UTC`` -- a no-op conversion -- so they keep asserting pure
bucketing logic unchanged; ``test_local_time_conversion_*`` pin a non-UTC
offset and assert the actually-converted wall-clock output.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from lode.tui.dates import format_adaptive_date

_NOW = datetime(2026, 7, 6, 15, 0, 0, tzinfo=UTC)  # a Monday


@contextmanager
def _local_tz(tz: str) -> Iterator[None]:
    """Pin the process's local timezone (``time.tzset``, POSIX-only) for a block.

    Restores whatever ``TZ`` was set to (or unsets it) on exit, so a test
    pinning a non-UTC offset can never leak into a later test.
    """
    original = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_today_renders_as_time_of_day() -> None:
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-07-06T14:30:00.000Z", now=_NOW) == "14:30"


def test_this_week_renders_as_weekday_and_time() -> None:
    # 2026-07-03 is a Friday, 3 days before the reference Monday.
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-07-03T09:12:00.000Z", now=_NOW) == "Fri 09:12"


def test_this_year_renders_as_month_and_day() -> None:
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-01-15T00:00:00.000Z", now=_NOW) == "Jan 15"


def test_older_than_a_year_renders_as_iso_date() -> None:
    with _local_tz("UTC"):
        assert (
            format_adaptive_date("2024-11-20T08:00:00.000Z", now=_NOW) == "2024-11-20"
        )


def test_last_year_renders_as_iso_date() -> None:
    with _local_tz("UTC"):
        assert (
            format_adaptive_date("2025-11-20T08:00:00.000Z", now=_NOW) == "2025-11-20"
        )


def test_exactly_one_day_ago_is_this_week_not_today() -> None:
    # Same clock time, one calendar day back -- must not fall in the "today" bucket.
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-07-05T15:00:00.000Z", now=_NOW) == "Sun 15:00"


def test_six_days_ago_is_still_this_week() -> None:
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-06-30T15:00:00.000Z", now=_NOW) == "Tue 15:00"


def test_seven_days_ago_falls_through_to_this_year() -> None:
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-06-29T15:00:00.000Z", now=_NOW) == "Jun 29"


def test_late_yesterday_is_this_week_not_today_despite_under_24h() -> None:
    # 11pm the previous calendar day, only ~16h before the 3pm reference --
    # bucketing is by calendar date, not elapsed hours.
    with _local_tz("UTC"):
        assert format_adaptive_date("2026-07-05T23:00:00.000Z", now=_NOW) == "Sun 23:00"


# --- local-time conversion (lode-olmi.5) ------------------------------------


def test_local_time_conversion_produces_correct_wall_clock() -> None:
    # Etc/GMT+5 is a fixed UTC-5 offset with no DST, so the expected output
    # never depends on which date DST would apply on.
    with _local_tz("Etc/GMT+5"):
        assert format_adaptive_date("2026-07-06T14:30:00.000Z", now=_NOW) == "09:30"


def test_local_time_conversion_can_shift_the_calendar_day() -> None:
    # A note captured just after midnight UTC is still "yesterday evening"
    # five hours west -- the whole point of converting before bucketing
    # rather than after formatting.
    with _local_tz("Etc/GMT+5"):
        assert format_adaptive_date("2026-07-06T03:00:00.000Z", now=_NOW) == "Sun 22:00"

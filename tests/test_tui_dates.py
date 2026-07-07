"""Tests for the browse screen's adaptive date formatter (lode-1gr.8).

Fixed ``now`` instants make every bucket deterministic: no real-clock flake,
no DB. One test per bucket (today, this week, this year, older/prior year),
plus the boundary cases the bucketing logic hinges on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lode.tui.dates import format_adaptive_date

_NOW = datetime(2026, 7, 6, 15, 0, 0, tzinfo=timezone.utc)  # a Monday


def test_today_renders_as_time_of_day() -> None:
    assert format_adaptive_date("2026-07-06T14:30:00.000Z", now=_NOW) == "14:30"


def test_this_week_renders_as_weekday_and_time() -> None:
    # 2026-07-03 is a Friday, 3 days before the reference Monday.
    assert format_adaptive_date("2026-07-03T09:12:00.000Z", now=_NOW) == "Fri 09:12"


def test_this_year_renders_as_month_and_day() -> None:
    assert format_adaptive_date("2026-01-15T00:00:00.000Z", now=_NOW) == "Jan 15"


def test_older_than_a_year_renders_as_iso_date() -> None:
    assert format_adaptive_date("2024-11-20T08:00:00.000Z", now=_NOW) == "2024-11-20"


def test_last_year_renders_as_iso_date() -> None:
    assert format_adaptive_date("2025-11-20T08:00:00.000Z", now=_NOW) == "2025-11-20"


def test_exactly_one_day_ago_is_this_week_not_today() -> None:
    # Same clock time, one calendar day back -- must not fall in the "today" bucket.
    assert format_adaptive_date("2026-07-05T15:00:00.000Z", now=_NOW) == "Sun 15:00"


def test_six_days_ago_is_still_this_week() -> None:
    assert format_adaptive_date("2026-06-30T15:00:00.000Z", now=_NOW) == "Tue 15:00"


def test_seven_days_ago_falls_through_to_this_year() -> None:
    assert format_adaptive_date("2026-06-29T15:00:00.000Z", now=_NOW) == "Jun 29"


def test_late_yesterday_is_this_week_not_today_despite_under_24h() -> None:
    # 11pm the previous calendar day, only ~16h before the 3pm reference --
    # bucketing is by calendar date, not elapsed hours.
    assert format_adaptive_date("2026-07-05T23:00:00.000Z", now=_NOW) == "Sun 23:00"

"""Tests for lode.ids -- the shared version-id abbreviation helper (lode-0bs).

Mirrors ``tests/test_notes_read.py``'s ``short_note_id`` tests: pins the
12-char truncation and the shorter-id passthrough so the shared helper's
behavior stays locked in wherever it's called from (worker.py, enrich.py,
staleness.py, cli._short).
"""

from lode.ids import SHORT_VERSION_ID_LENGTH, short_version_id


def test_short_version_id_truncates_to_12_chars() -> None:
    assert SHORT_VERSION_ID_LENGTH == 12
    assert short_version_id("0123456789abcdef") == "0123456789ab"


def test_short_version_id_leaves_a_shorter_id_unchanged() -> None:
    assert short_version_id("short") == "short"

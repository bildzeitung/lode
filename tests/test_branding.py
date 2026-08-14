"""Tests for lode.branding (lode-fhql.5) -- the wordmark's own module.

Covers the two hard constraints the ticket calls out directly: the 80-column
limit (CaptureScreen overflowed it once, lode-3rvw) and an EXPLICIT
Unicode/ASCII selection (decided from the target stream's encoding before
anything is printed, never emitted-and-hoped-for).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from lode.branding import (
    TAGLINE,
    WORDMARK_ASCII,
    WORDMARK_UNICODE,
    supports_unicode,
    wordmark,
)


def test_both_wordmark_forms_share_one_22x5_footprint() -> None:
    # The module docstring promises a 22x5 footprint for BOTH forms, so
    # callers never special-case layout on which one was picked -- and 22 is
    # what keeps the mark inside the ticket's 80-column hard limit
    # (CaptureScreen overflowed that once, lode-3rvw).
    unicode_rows = WORDMARK_UNICODE.splitlines()
    ascii_rows = WORDMARK_ASCII.splitlines()
    assert len(unicode_rows) == len(ascii_rows) == 5
    assert {len(row) for row in unicode_rows + ascii_rows} == {22}


def test_tagline_is_plain_ascii() -> None:
    TAGLINE.encode("ascii")  # raises UnicodeEncodeError if not


def test_wordmark_ascii_form_is_plain_ascii() -> None:
    WORDMARK_ASCII.encode("ascii")


def test_wordmark_unicode_form_is_not_plain_ascii() -> None:
    # The two forms must actually differ (otherwise the "ASCII fallback" is
    # a no-op), and the Unicode form must genuinely be non-ASCII -- which is
    # the whole reason the fallback exists.
    assert WORDMARK_UNICODE != WORDMARK_ASCII
    with pytest.raises(UnicodeEncodeError):
        WORDMARK_UNICODE.encode("ascii")


def test_supports_unicode_true_for_utf8_stream() -> None:
    stream = SimpleNamespace(encoding="utf-8")
    assert supports_unicode(stream) is True


def test_supports_unicode_false_for_ascii_stream() -> None:
    stream = SimpleNamespace(encoding="ascii")
    assert supports_unicode(stream) is False


def test_supports_unicode_false_when_encoding_is_unknown() -> None:
    # A stream with no `encoding` attribute at all (e.g. some in-memory /
    # test doubles) must not be assumed Unicode-capable.
    stream = object()
    assert supports_unicode(stream) is False  # type: ignore[arg-type]


def test_wordmark_default_follows_stdouts_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default path is the CLI's: no `unicode=` argument, so the form is
    # decided by sys.stdout's own declared encoding.
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(encoding="utf-8"))
    assert WORDMARK_UNICODE in wordmark()

    monkeypatch.setattr(sys, "stdout", SimpleNamespace(encoding="ascii"))
    assert WORDMARK_ASCII in wordmark()


def test_wordmark_explicit_override_bypasses_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An explicit `unicode=` wins over whatever sys.stdout declares -- this
    # is how the TUI pins the Unicode form (Textual, not sys.stdout, owns
    # its render target).
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(encoding="ascii"))
    assert WORDMARK_UNICODE in wordmark(unicode=True)

    monkeypatch.setattr(sys, "stdout", SimpleNamespace(encoding="utf-8"))
    assert WORDMARK_ASCII in wordmark(unicode=False)


def test_wordmark_includes_tagline() -> None:
    assert TAGLINE in wordmark(unicode=True)
    assert TAGLINE in wordmark(unicode=False)


def test_wordmark_carries_no_ansi_escape_codes() -> None:
    # No colour in this module at all (see the module docstring) -- NO_COLOR
    # and a non-TTY pipe need no special-casing because there's nothing to
    # suppress.
    assert "\x1b" not in wordmark(unicode=True)
    assert "\x1b" not in wordmark(unicode=False)

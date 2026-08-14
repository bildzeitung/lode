"""Tests for lode.branding (lode-fhql.5) -- the wordmark's own module.

Covers the two hard constraints the ticket calls out directly: the 80-column
limit (CaptureScreen overflowed it once, lode-3rvw) and an EXPLICIT
Unicode/ASCII selection (decided from the target stream's encoding before
anything is printed, never emitted-and-hoped-for).
"""

from __future__ import annotations

from types import SimpleNamespace

from lode.branding import (
    TAGLINE,
    WORDMARK_ASCII,
    WORDMARK_UNICODE,
    supports_unicode,
    wordmark,
)


def test_both_wordmark_forms_fit_in_80_columns() -> None:
    for glyphs in (WORDMARK_UNICODE, WORDMARK_ASCII):
        for line in glyphs.splitlines():
            assert len(line) <= 80


def test_tagline_is_plain_ascii() -> None:
    TAGLINE.encode("ascii")  # raises UnicodeEncodeError if not


def test_wordmark_ascii_form_is_plain_ascii() -> None:
    WORDMARK_ASCII.encode("ascii")


def test_wordmark_unicode_form_is_not_plain_ascii() -> None:
    # Sanity: the two forms are actually different, not just aliases of one
    # another -- otherwise "ASCII fallback" would be a no-op.
    assert WORDMARK_UNICODE != WORDMARK_ASCII


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


def test_wordmark_explicit_override_bypasses_detection() -> None:
    ascii_stream = SimpleNamespace(encoding="ascii")
    result = wordmark(unicode=True, stream=ascii_stream)
    assert WORDMARK_UNICODE in result

    utf8_stream = SimpleNamespace(encoding="utf-8")
    result = wordmark(unicode=False, stream=utf8_stream)
    assert WORDMARK_ASCII in result


def test_wordmark_includes_tagline() -> None:
    assert TAGLINE in wordmark(unicode=True)
    assert TAGLINE in wordmark(unicode=False)


def test_wordmark_carries_no_ansi_escape_codes() -> None:
    # No colour in this module at all (see the module docstring) -- NO_COLOR
    # and a non-TTY pipe need no special-casing because there's nothing to
    # suppress.
    assert "\x1b" not in wordmark(unicode=True)
    assert "\x1b" not in wordmark(unicode=False)

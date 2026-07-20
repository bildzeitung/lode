"""Pure-function tests for the open-link-under-cursor feature (lode-ev5j.3).

:func:`~lode.tui.screens._link_open.extract_link_at_cursor` and
:func:`~lode.tui.screens._link_open.resolve_link_open` are both plain
functions -- no Textual/Screen/IO dependency -- so they're tested here in
isolation from any live widget, per this ticket's own Testing note. Screen
wiring (the Ctrl+N binding on ``EditScreen``/``VersionViewScreen``/
``SnapshotViewerScreen``) is covered separately by the TUI screen tests.
"""

from __future__ import annotations

import webbrowser

import pytest

from lode.tui.screens import _link_open
from lode.tui.screens._link_open import extract_link_at_cursor, resolve_link_open


#: A stand-in for a real GUI-browser controller (Firefox/Chrome/etc.) -- any
#: type that is not `webbrowser.GenericBrowser` counts as "safe" per
#: `resolve_link_open`'s contract, so a bare unrelated class is enough.
class _FakeGuiBrowser:
    pass


#: `webbrowser.BackgroundBrowser` SUBCLASSES `GenericBrowser` in the real
#: stdlib and is safe (it does not share the tty) -- this fake mirrors that
#: relationship without depending on stdlib platform specifics, to pin that
#: `resolve_link_open` checks the EXACT type, never `isinstance`.
class _FakeBackgroundBrowser(webbrowser.GenericBrowser):
    pass


# ---------------------------------------------------------------------------
# extract_link_at_cursor -- inline links
# ---------------------------------------------------------------------------


def test_inline_link_cursor_inside_matches() -> None:
    line = "see [my link](https://example.com/path) for more"
    # column 10 lands inside "my link", well within the "[...](...)" span.
    assert extract_link_at_cursor(line, 10) == "https://example.com/path"


def test_inline_link_cursor_on_the_url_itself_matches() -> None:
    line = "see [my link](https://example.com/path) for more"
    url_column = line.index("https")
    assert extract_link_at_cursor(line, url_column) == "https://example.com/path"


def test_inline_link_cursor_before_the_link_does_not_match() -> None:
    line = "see [my link](https://example.com/path) for more"
    link_start = line.index("[")
    assert extract_link_at_cursor(line, link_start - 1) is None


def test_inline_link_cursor_exactly_at_link_start_matches() -> None:
    line = "[my link](https://example.com/path)"
    assert extract_link_at_cursor(line, 0) == "https://example.com/path"


def test_inline_link_cursor_after_the_link_does_not_match() -> None:
    line = "see [my link](https://example.com/path) for more"
    link_end = line.index(")") + 1
    # one column past the closing paren -- "after," per this function's own
    # start <= column < end contract.
    assert extract_link_at_cursor(line, link_end) is None


# ---------------------------------------------------------------------------
# extract_link_at_cursor -- reference-style link definitions
# ---------------------------------------------------------------------------


def test_reference_link_definition_cursor_on_label_matches() -> None:
    line = "[my label]: https://example.com/ref"
    assert extract_link_at_cursor(line, 2) == "https://example.com/ref"


def test_reference_link_definition_cursor_on_url_matches() -> None:
    line = "[my label]: https://example.com/ref"
    url_column = line.index("https")
    assert extract_link_at_cursor(line, url_column) == "https://example.com/ref"


def test_reference_link_definition_with_leading_whitespace_matches() -> None:
    line = "  [label]: https://example.com/indented"
    assert extract_link_at_cursor(line, 4) == "https://example.com/indented"


# ---------------------------------------------------------------------------
# extract_link_at_cursor -- bare URLs
# ---------------------------------------------------------------------------


def test_bare_url_cursor_inside_matches() -> None:
    line = "go to https://example.com/bare now"
    column = line.index("example")
    assert extract_link_at_cursor(line, column) == "https://example.com/bare"


def test_bare_url_cursor_before_does_not_match() -> None:
    line = "go to https://example.com/bare now"
    url_start = line.index("https")
    assert extract_link_at_cursor(line, url_start - 1) is None


def test_bare_url_cursor_after_does_not_match() -> None:
    line = "go to https://example.com/bare now"
    url_end = line.index("https://example.com/bare") + len("https://example.com/bare")
    assert extract_link_at_cursor(line, url_end) is None


# ---------------------------------------------------------------------------
# extract_link_at_cursor -- prose punctuation and parens in the URL itself
#
# Regression cover for the review of lode-ev5j.3. The original bare-URL
# matcher here was a local `https?://\S+` with no trimming, which disagreed
# with `drawdown.extract_urls` -- the matcher that decides which URLs the SAME
# note body opens external edges for. Ctrl+N therefore opened a URL that
# differed from the recorded external for that very character position:
# a trailing sentence period, a wrapping paren, or (worst) a Wikipedia
# `Foo_(bar)` target truncated mid-path. Bare-URL matching now delegates to
# `drawdown.iter_url_spans`, so the two agree by construction; these tests
# pin the agreement rather than re-testing drawdown's own trim rule.
# ---------------------------------------------------------------------------


def test_bare_url_trailing_sentence_period_is_not_part_of_the_url() -> None:
    line = "see https://example.com/foo."
    column = line.index("example")
    assert extract_link_at_cursor(line, column) == "https://example.com/foo"


def test_cursor_on_the_trimmed_trailing_period_matches_nothing() -> None:
    line = "see https://example.com/foo."
    assert extract_link_at_cursor(line, line.index(".", line.index("/foo"))) is None


def test_bare_url_wrapped_in_prose_parens_drops_the_closing_paren() -> None:
    line = "the source (https://example.com/foo) says otherwise"
    column = line.index("example")
    assert extract_link_at_cursor(line, column) == "https://example.com/foo"


def test_bare_url_keeps_balanced_parens_inside_its_own_path() -> None:
    line = "see https://en.wikipedia.org/wiki/Mercury_(planet) for more"
    column = line.index("wiki")
    assert (
        extract_link_at_cursor(line, column)
        == "https://en.wikipedia.org/wiki/Mercury_(planet)"
    )


def test_inline_link_target_may_contain_balanced_parens() -> None:
    line = "[Mercury](https://en.wikipedia.org/wiki/Mercury_(planet))"
    expected = "https://en.wikipedia.org/wiki/Mercury_(planet)"
    # Both on the visible link TEXT and on the target itself -- the whole
    # `[text](url)` construct is one span, so the cursor may sit anywhere in it.
    assert extract_link_at_cursor(line, line.index("Mercury")) == expected
    assert extract_link_at_cursor(line, line.index("wiki")) == expected


def test_inline_link_target_with_a_parenthesised_query_value() -> None:
    line = "[q](https://example.com/s?f=(a,b))"
    assert extract_link_at_cursor(line, 1) == "https://example.com/s?f=(a,b)"


def test_bare_url_inside_an_inline_link_does_not_shadow_the_inline_match() -> None:
    # Precedence: the inline shape is tried first, so the URL substring inside
    # the parens can never win with a differently-trimmed span.
    line = "[docs](https://example.com/a.)"
    assert extract_link_at_cursor(line, line.index("docs")) == "https://example.com/a."


# ---------------------------------------------------------------------------
# extract_link_at_cursor -- no link, multiple links
# ---------------------------------------------------------------------------


def test_no_link_on_the_line_returns_none() -> None:
    line = "just a plain sentence with no links at all"
    assert extract_link_at_cursor(line, 5) is None


def test_empty_line_returns_none() -> None:
    assert extract_link_at_cursor("", 0) is None


def test_multiple_links_on_one_line_each_resolve_independently() -> None:
    line = "[first](https://one.example) and [second](https://two.example)"
    first_column = line.index("first")
    second_column = line.index("second")
    assert extract_link_at_cursor(line, first_column) == "https://one.example"
    assert extract_link_at_cursor(line, second_column) == "https://two.example"


def test_cursor_between_two_links_on_one_line_matches_neither() -> None:
    line = "[first](https://one.example) and [second](https://two.example)"
    between_column = line.index(" and ") + 2
    assert extract_link_at_cursor(line, between_column) is None


# ---------------------------------------------------------------------------
# resolve_link_open -- the controller-type / display safety predicate
# (lode-ev5j.3's browser-safety review superseded the original $BROWSER
# denylist wording with an exact-controller-type check; see the module
# docstring and this ticket's notes for why a name list can't catch every
# terminal browser.)
# ---------------------------------------------------------------------------


def test_normal_env_with_display_opens() -> None:
    should_open, message = resolve_link_open(
        "https://example.com", {"DISPLAY": ":0"}, controller_type=_FakeGuiBrowser
    )
    assert should_open is True
    assert "https://example.com" in message


def test_generic_browser_controller_refuses_even_with_a_display() -> None:
    should_open, message = resolve_link_open(
        "https://example.com",
        {"DISPLAY": ":0"},
        controller_type=webbrowser.GenericBrowser,
    )
    assert should_open is False
    assert "https://example.com" in message


def test_generic_browser_subclass_is_treated_as_safe_not_via_isinstance() -> None:
    # BackgroundBrowser subclasses GenericBrowser in the real stdlib and does
    # NOT share the tty -- the check must be an exact type comparison, never
    # isinstance, or this would wrongly refuse a safe controller.
    should_open, _ = resolve_link_open(
        "https://example.com",
        {"DISPLAY": ":0"},
        controller_type=_FakeBackgroundBrowser,
    )
    assert should_open is True


def test_controller_type_none_refuses() -> None:
    # None means webbrowser.get() raised webbrowser.Error -- no controller
    # could be resolved at all, so there is nothing safe to open.
    should_open, message = resolve_link_open(
        "https://example.com", {"DISPLAY": ":0"}, controller_type=None
    )
    assert should_open is False
    assert "https://example.com" in message


def test_no_display_and_no_ssh_x11_refuses() -> None:
    should_open, message = resolve_link_open(
        "https://example.com", {}, controller_type=_FakeGuiBrowser
    )
    assert should_open is False
    assert "https://example.com" in message


def test_wayland_display_counts_as_a_display() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com",
        {"WAYLAND_DISPLAY": "wayland-0"},
        controller_type=_FakeGuiBrowser,
    )
    assert should_open is True


def test_macos_never_needs_display() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com", {}, controller_type=_FakeGuiBrowser, is_macos=True
    )
    assert should_open is True


def test_macos_still_refuses_a_generic_browser_controller() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com",
        {},
        controller_type=webbrowser.GenericBrowser,
        is_macos=True,
    )
    assert should_open is False


# ---------------------------------------------------------------------------
# open_link_under_cursor -- the widget read happens on the CALLING thread
# ---------------------------------------------------------------------------


class _FakeDocument:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def get_line(self, row: int) -> str:
        return self._lines[row]


class _FakeTextArea:
    def __init__(self, lines: list[str], cursor: tuple[int, int]) -> None:
        self.document = _FakeDocument(lines)
        self.cursor_location = cursor


class _FakeScreen:
    """A screen that is deliberately NOT a Textual ``DOMNode``.

    That is the whole point of the two tests below: `@work` asserts its first
    argument is a `DOMNode` and immediately hands the call to `run_worker`, so
    a plain object like this can only get through `open_link_under_cursor` if
    that function does its widget read and its no-link notify *synchronously*,
    on the calling thread, rather than deferring them onto a worker.
    """

    def __init__(self) -> None:
        self.notified: list[tuple[str, str | None]] = []

    def notify(self, message: str, severity: str | None = None) -> None:
        self.notified.append((message, severity))


def test_open_link_under_cursor_extracts_the_url_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor/document read must NOT be deferred onto the worker thread.

    A thread worker starts asynchronously, so reading a live (editable)
    `TextArea` from inside it races the user's own typing -- it can extract a
    URL from a line the cursor has since left, and `get_line(row)` can raise
    `IndexError` outright on a since-shortened document, which (workers
    default to `exit_on_error=True`) takes the whole app down. Only the
    blocking browser work belongs on the thread.
    """
    captured: list[str] = []
    monkeypatch.setattr(
        _link_open, "_open_url", lambda screen, url: captured.append(url)
    )
    screen = _FakeScreen()
    text_area = _FakeTextArea(
        ["see [my link](https://example.com/path) please"], (0, 8)
    )

    _link_open.open_link_under_cursor(screen, text_area)  # type: ignore[arg-type]

    assert captured == ["https://example.com/path"]
    assert screen.notified == []


def test_open_link_under_cursor_notifies_synchronously_when_there_is_no_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        _link_open, "_open_url", lambda screen, url: captured.append(url)
    )
    screen = _FakeScreen()
    text_area = _FakeTextArea(["no links on this line at all"], (0, 3))

    _link_open.open_link_under_cursor(screen, text_area)  # type: ignore[arg-type]

    assert captured == []  # no browser work dispatched at all
    assert screen.notified == [("no link under the cursor", "warning")]

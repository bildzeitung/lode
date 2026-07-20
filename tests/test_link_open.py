"""Pure-function tests for the open-link-under-cursor feature (lode-ev5j.3).

:func:`~lode.tui.screens._link_open.extract_link_at_cursor` and
:func:`~lode.tui.screens._link_open.resolve_link_open` are both plain
functions -- no Textual/Screen/IO dependency -- so they're tested here in
isolation from any live widget, per this ticket's own Testing note. Screen
wiring (the Ctrl+N binding on ``EditScreen``/``VersionViewScreen``/
``SnapshotViewerScreen``) is covered separately by the TUI screen tests.
"""

from __future__ import annotations

from lode.tui.screens._link_open import extract_link_at_cursor, resolve_link_open

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
# resolve_link_open -- the $BROWSER / display safety predicate
# ---------------------------------------------------------------------------


def test_normal_env_with_display_opens() -> None:
    should_open, message = resolve_link_open(
        "https://example.com", {"DISPLAY": ":0"}
    )
    assert should_open is True
    assert "https://example.com" in message


def test_browser_w3m_refuses_even_with_a_display() -> None:
    should_open, message = resolve_link_open(
        "https://example.com", {"DISPLAY": ":0", "BROWSER": "w3m"}
    )
    assert should_open is False
    assert "https://example.com" in message


def test_browser_terminal_denylist_covers_lynx_links_elinks() -> None:
    for terminal_browser in ("lynx", "links", "elinks"):
        should_open, message = resolve_link_open(
            "https://example.com",
            {"DISPLAY": ":0", "BROWSER": terminal_browser},
        )
        assert should_open is False, terminal_browser
        assert "https://example.com" in message


def test_browser_terminal_denylist_matches_a_full_path() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com", {"DISPLAY": ":0", "BROWSER": "/usr/bin/w3m"}
    )
    assert should_open is False


def test_browser_colon_separated_list_checks_every_entry() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com",
        {"DISPLAY": ":0", "BROWSER": "firefox:w3m"},
    )
    assert should_open is False


def test_no_display_and_no_ssh_x11_refuses() -> None:
    should_open, message = resolve_link_open("https://example.com", {})
    assert should_open is False
    assert "https://example.com" in message


def test_wayland_display_counts_as_a_display() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com", {"WAYLAND_DISPLAY": "wayland-0"}
    )
    assert should_open is True


def test_macos_never_needs_display() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com", {}, is_macos=True
    )
    assert should_open is True


def test_macos_still_refuses_a_terminal_browser() -> None:
    should_open, _ = resolve_link_open(
        "https://example.com", {"BROWSER": "lynx"}, is_macos=True
    )
    assert should_open is False


def test_unset_browser_with_display_opens() -> None:
    should_open, _ = resolve_link_open("https://example.com", {"DISPLAY": ":1"})
    assert should_open is True

"""Open-link-under-cursor: pure extraction + browser-safety helpers (lode-ev5j.3).

Feature 3 of the `lode-ev5j` markdown epic: a keybinding that opens the URL
under the cursor in the system browser, replacing mouse-clickable links
(impossible on stock Textual -- see `lode-ev5j`'s own decision note).

**No dependency on lode-ev5j.2's syntax colouring.** `lode-ev5j.1`'s spike
found textual's bundled `markdown.scm` only reaches BLOCK-grammar link nodes
(reference definitions like `[label]: url`), never inline `[text](url)` --
so there is no parse-tree node to read at the cursor for an ordinary inline
link. This module scans the cursor's own line with plain regexes instead,
which is both simpler and independent of whatever highlighting exists.

Split into a leaf module (underscore-prefixed per `docs/conventions.md` --
it hosts no `Screen`/`Widget` of its own, so it doesn't count against the
one-Screen/Widget-per-module fiat), shared by
:class:`~lode.tui.screens.edit.EditScreen`,
:class:`~lode.tui.screens.version_view.VersionViewScreen`, and
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` -- the same
three screens `lode-ev5j.2` targets -- so the extraction and browser-guard
logic lives in exactly one place rather than being reimplemented per screen.

**Two pieces, each a pure function, on purpose (see this ticket's own
Testing note).** :func:`extract_link_at_cursor` is line text + cursor column
-> URL-or-``None``, no Textual/IO dependency at all. :func:`resolve_link_open`
is env mapping (+ platform) -> should-open + status message, equally pure.
:func:`open_link_under_cursor` is the only piece that touches a live
``Screen``/``TextArea`` -- it just wires the two pure functions together and
calls ``webbrowser.open``/``screen.notify``.
"""

from __future__ import annotations

import os
import re
import sys
import webbrowser
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.screen import Screen
    from textual.widgets import TextArea

#: Terminal browsers that must never be launched from inside a running TUI --
#: doing so replaces the CURRENT tty's contents with the terminal browser,
#: corrupting the TUI underneath it rather than opening a separate window
#: (this ticket's own design note).
_TERMINAL_BROWSER_DENYLIST = frozenset({"w3m", "lynx", "links", "elinks"})

#: `[text](url)` -- the whole construct (brackets, text, parens, url) is the
#: match span, so a cursor anywhere in the visible link -- not just over the
#: raw URL -- counts as "on" it.
_INLINE_LINK_RE = re.compile(r"\[[^\[\]]*\]\(([^()\s]+)\)")

#: `[label]: url` -- a reference-style link *definition*, the one shape
#: textual's own block-grammar `markdown.scm` can already see (lode-ev5j.1's
#: spike). The whole line is the match span -- there is nothing else useful
#: on a reference-definition line.
_REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\[\]]+\]:\s*(\S+)")

#: A bare URL with no markdown syntax around it at all.
_BARE_URL_RE = re.compile(r"https?://\S+")


def extract_link_at_cursor(line: str, column: int) -> str | None:
    """The URL under *column* on *line*, or ``None`` if the cursor is on no link.

    Pure function -- no Textual/Screen/IO dependency -- so it's unit
    testable on its own (this ticket's Testing note). Covers the three
    shapes a note body can hold a link as: an inline link (`[text](url)`), a
    reference-style link definition (`[label]: url`), and a bare URL.

    *column* must fall strictly within the matched span
    (``start <= column < end``): landing exactly one column past the link's
    last character does not count as "on" it, matching how a text cursor
    sits *between* characters. When several links share one line, the first
    whose span contains *column* wins -- their spans cannot legitimately
    overlap for well-formed markdown.
    """
    candidates: list[tuple[int, int, str]] = []

    for match in _INLINE_LINK_RE.finditer(line):
        candidates.append((match.start(), match.end(), match.group(1)))

    reference_match = _REFERENCE_LINK_RE.match(line)
    if reference_match is not None:
        candidates.append(
            (reference_match.start(), reference_match.end(), reference_match.group(1))
        )

    for match in _BARE_URL_RE.finditer(line):
        # Skip a bare-URL match already covered by an inline/reference span
        # above (e.g. the URL substring inside `[text](url)`) -- it would
        # just duplicate that span's url for no benefit.
        if any(start <= match.start() < end for start, end, _ in candidates):
            continue
        candidates.append((match.start(), match.end(), match.group(0)))

    for start, end, url in candidates:
        if start <= column < end:
            return url
    return None


def _is_terminal_browser(command: str) -> bool:
    """True if *command* (one `:`-separated `$BROWSER` entry) names a terminal browser."""
    first_token = command.strip().split()[0] if command.strip() else ""
    name = os.path.basename(first_token).lower()
    return name in _TERMINAL_BROWSER_DENYLIST


def _has_display(env: Mapping[str, str], *, is_macos: bool) -> bool:
    """True if a GUI display is plausibly reachable from *env*.

    macOS never needs `$DISPLAY` (Cocoa apps don't use X11), so *is_macos*
    short-circuits straight to ``True`` there. Everywhere else: an X11
    `$DISPLAY` or a Wayland `$WAYLAND_DISPLAY` set means a compositor is
    reachable; neither set (the common SSH-without-forwarding /
    headless-server case) means it is not.
    """
    if is_macos:
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def resolve_link_open(
    url: str, env: Mapping[str, str], *, is_macos: bool = False
) -> tuple[bool, str]:
    """Decide whether *url* is safe to hand to ``webbrowser.open`` under *env*.

    Returns ``(should_open, status_message)``. *status_message* always
    carries *url*, whichever branch fires -- opened or refused -- so it is
    always manually copyable off the status line (this ticket's "never
    silently no-op" acceptance criterion). Pure function: no ``webbrowser``
    call, no live environment read -- *env* is passed in, so this is
    testable against a fake mapping.

    Checked in order: a `$BROWSER` naming a terminal browser refuses
    regardless of display (opening it would corrupt the running TUI, exactly
    as unsafe over SSH-with-X11-forwarding as it is on a bare local
    terminal); otherwise, no reachable display refuses too (over SSH without
    forwarding, or headless); otherwise it's safe to open.
    """
    browser_cmd = env.get("BROWSER", "")
    if browser_cmd and any(
        _is_terminal_browser(part) for part in browser_cmd.split(":") if part
    ):
        return False, f"$BROWSER is a terminal browser -- link: {url}"
    if not _has_display(env, is_macos=is_macos):
        return False, f"no display available -- link: {url}"
    return True, f"opened in browser -- link: {url}"


def open_link_under_cursor(screen: Screen[object], text_area: TextArea) -> None:
    """Ctrl+N: open the link under *text_area*'s cursor, or explain there isn't one.

    Shared glue between the two pure functions above and a live screen:
    reads the cursor's own line out of *text_area*, extracts a URL (if any),
    resolves whether it's safe to open against the real process environment,
    and notifies -- opening the browser first when it's safe, so a
    conflicting title inherited from a previous state can't race the visible
    status message.
    """
    row, column = text_area.cursor_location
    line = text_area.document.get_line(row)
    url = extract_link_at_cursor(line, column)
    if url is None:
        screen.notify("no link under the cursor", severity="warning")
        return
    should_open, message = resolve_link_open(
        url, os.environ, is_macos=sys.platform == "darwin"
    )
    if should_open:
        webbrowser.open(url)
    screen.notify(message)

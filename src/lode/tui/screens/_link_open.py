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

**Bare URLs are matched by :func:`lode.drawdown.iter_url_spans`, not by a
regex of this module's own.** That is the same matcher (and the same
trailing-prose-punctuation trimming) that decides which URLs a note body
opens *external edges* for, and the two must not disagree: a second regex
here means Ctrl+N can open a different URL than the one lode recorded as the
external for that very character position. Only the two markdown shapes
below -- which drawdown has no interest in -- are matched locally.

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
is env mapping + resolved browser-controller type (+ platform) -> should-open
+ status message, equally pure -- the live ``webbrowser.get()`` call that
resolves the controller is made by :func:`open_link_under_cursor`, not here.
:func:`open_link_under_cursor` is the only piece that touches a live
``Screen``/``TextArea`` or a real browser -- it wires the two pure functions
together, calls ``webbrowser.get``/``webbrowser.open``/``screen.notify``, and
(lode-ev5j.3's browser-safety review) runs entirely on a worker THREAD via
``@work(thread=True)`` -- see its own docstring for why.

**Browser-safety guard: exact controller type, not a ``$BROWSER`` name list
(lode-ev5j.3's browser-safety review, superseding this ticket's original
denylist wording).** A name list can only refuse browsers it happens to
enumerate; it can't see a name it doesn't know. Verified against the CPython
stdlib: ``webbrowser.register_standard_browsers()`` registers console
browsers (``www-browser``, ``links``, ``lynx``, ``w3m``) as
``GenericBrowser`` whenever ``$TERM`` is set, INDEPENDENT of ``$DISPLAY`` and
with ``$BROWSER`` never involved -- so a slim container/devcontainer with
only text browsers on ``PATH`` resolves ``GenericBrowser('www-browser')``
even with ``$DISPLAY`` set and no denylist match. ``GenericBrowser.open()``
does ``subprocess.Popen(...)`` then ``p.wait()`` -- foreground, sharing the
current tty, exactly the corruption this guard exists to prevent. The fix:
refuse whenever the controller that would actually run is EXACTLY
``webbrowser.GenericBrowser`` -- ``type(webbrowser.get()) is
webbrowser.GenericBrowser``, never ``isinstance`` (``BackgroundBrowser``
subclasses ``GenericBrowser`` and is safe -- it does not share the tty). This
subsumes all four originally-denylisted names by construction, and closes
the reachable gap a name list structurally cannot: an unrecognized
``$BROWSER`` value (a wrapper script, ``browsh``, ``carbonyl``, ...) also
resolves to ``GenericBrowser`` and is caught the same way.
"""

from __future__ import annotations

import os
import re
import sys
import webbrowser
from collections.abc import Mapping
from typing import TYPE_CHECKING

from textual import work

from lode.drawdown import iter_url_spans

if TYPE_CHECKING:
    from textual.screen import Screen
    from textual.widgets import TextArea

#: `[text](url)` -- the whole construct (brackets, text, parens, url) is the
#: match span, so a cursor anywhere in the visible link -- not just over the
#: raw URL -- counts as "on" it.
#:
#: The target allows ONE level of balanced parens (`\([^()\s]*\)`) as well as
#: paren-free runs, so a Wikipedia-style `.../wiki/Foo_(bar)` target matches
#: as an inline link rather than falling through to the bare-URL scan -- which
#: would still find the URL, but only with the cursor on the URL itself, not
#: on the link *text*. One level is deliberate: it covers the disambiguation
#: -suffix case that actually occurs in notes, and a fully balanced matcher
#: isn't expressible as a regex.
_INLINE_LINK_RE = re.compile(r"\[[^\[\]]*\]\(((?:[^()\s]|\([^()\s]*\))+)\)")

#: `[label]: url` -- a reference-style link *definition*, the one shape
#: textual's own block-grammar `markdown.scm` can already see (lode-ev5j.1's
#: spike). The whole line is the match span -- there is nothing else useful
#: on a reference-definition line.
_REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\[\]]+\]:\s*(\S+)")


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

    The three shapes are tried in precedence order, returning on the first
    hit, so a bare URL nested inside an inline link's parens never shadows
    the inline match that contains it.
    """
    for match in _INLINE_LINK_RE.finditer(line):
        if match.start() <= column < match.end():
            return match.group(1)

    reference_match = _REFERENCE_LINK_RE.match(line)
    if reference_match is not None and column < reference_match.end():
        return reference_match.group(1)

    # Bare-URL spans come from `drawdown.iter_url_spans` -- the SAME matcher
    # and trailing-prose-punctuation trimming that decides which URLs a note
    # body opens external edges for (`drawdown.extract_urls`). Sharing it is
    # load-bearing, not tidiness: a second regex here drifted from that one
    # (plain `https?://\S+`, no trimming), so `see https://example.com.` fed
    # Ctrl+N the trailing period and `(https://example.com)` the wrapping
    # paren -- opening a URL that differed from the one lode recorded as the
    # external edge for that very character position.
    for start, end, url in iter_url_spans(line):
        if start <= column < end:
            return url
    return None


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
    url: str,
    env: Mapping[str, str],
    *,
    controller_type: type[webbrowser.BaseBrowser] | None,
    is_macos: bool = False,
) -> tuple[bool, str]:
    """Decide whether *url* is safe to hand to ``webbrowser.open`` under *env*.

    Returns ``(should_open, status_message)``. *status_message* always
    carries *url*, whichever branch fires -- opened or refused -- so it is
    always manually copyable off the status line (this ticket's "never
    silently no-op" acceptance criterion). Pure function: no ``webbrowser``
    call, no live environment read -- *env* and *controller_type* (the type
    of whatever ``webbrowser.get()`` resolved to, or ``None`` if resolving it
    raised ``webbrowser.Error``) are passed in, so this is testable against a
    fake mapping and a fake type, never a live browser lookup.

    Checked in order: a controller that resolves to EXACTLY
    ``webbrowser.GenericBrowser`` (or that failed to resolve at all) refuses
    regardless of display -- opening it would corrupt the running TUI, exactly
    as unsafe over SSH-with-X11-forwarding as it is on a bare local terminal
    (see the module docstring for why this must be an exact type check, not
    ``isinstance``); otherwise, no reachable display refuses too (over SSH
    without forwarding, or headless -- a GUI browser installed but nothing to
    show it on still resolves a safe controller, so this check stays
    independent of the one above); otherwise it's safe to open.
    """
    if controller_type is None or controller_type is webbrowser.GenericBrowser:
        return False, f"browser would open in this terminal -- link: {url}"
    if not _has_display(env, is_macos=is_macos):
        return False, f"no display available -- link: {url}"
    return True, f"opened in browser -- link: {url}"


@work(thread=True)
def open_link_under_cursor(screen: Screen[object], text_area: TextArea) -> None:
    """Ctrl+N: open the link under *text_area*'s cursor, or explain there isn't one.

    Shared glue between the two pure functions above and a live screen:
    reads the cursor's own line out of *text_area*, extracts a URL (if any),
    resolves the live browser controller and whether it's safe to open
    against the real process environment, and notifies -- opening the
    browser first when it's safe, so a conflicting title inherited from a
    previous state can't race the visible status message.

    Runs entirely on a worker THREAD (lode-ev5j.3's browser-safety review) --
    three verified stdlib paths block for real: resolving the controller via
    ``webbrowser.get()`` runs ``subprocess.check_output(['xdg-settings', ...])``
    with no timeout on its first call; ``UnixBrowser._invoke`` hard-freezes
    for up to 5s launching a cold browser; ``GenericBrowser.open`` blocks for
    the browser's entire foreground lifetime. None of that may run on the
    Textual event loop, so the whole thing moves off it, mirroring
    :meth:`~lode.tui.screens.ask.AskScreen._ask`'s own ``@work(thread=True)``
    pattern -- every ``notify`` accordingly goes through
    ``screen.app.call_from_thread``, never called directly from this thread.
    """
    row, column = text_area.cursor_location
    line = text_area.document.get_line(row)
    url = extract_link_at_cursor(line, column)
    if url is None:
        screen.app.call_from_thread(
            screen.notify, "no link under the cursor", severity="warning"
        )
        return
    try:
        controller_type: type[webbrowser.BaseBrowser] | None = type(webbrowser.get())
    except webbrowser.Error:
        controller_type = None
    should_open, message = resolve_link_open(
        url,
        os.environ,
        controller_type=controller_type,
        is_macos=sys.platform == "darwin",
    )
    if should_open:
        webbrowser.open(url)
    screen.app.call_from_thread(screen.notify, message)

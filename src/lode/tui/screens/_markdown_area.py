"""Construct a note-body ``TextArea`` with live markdown syntax colouring (lode-ev5j.2).

Shared by the three screens that show a note body as markdown --
:class:`~lode.tui.screens.edit.EditScreen` (``EDIT_BODY_ID``, editable),
:class:`~lode.tui.screens.version_view.VersionViewScreen` (``VERSION_BODY_ID``,
read-only), and :class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen`
(``SNAPSHOT_VIEWER_BODY_ID``, read-only) -- so the graceful-degradation
try/except lives in exactly one place rather than three copies (the ticket's
own instruction). :class:`~lode.tui.screens.reconcile.ReconcileScreen` is
deliberately **not** a caller: it renders a diff, not markdown, and colouring
would fight the diff structure.

**Colour depth is block-only**, per the lode-ev5j.1 spike: ``language="markdown"``
loads only Textual's bundled block grammar (headings, heading markers,
fenced/indented code, fence delimiters, list markers, block-quote markers,
thematic breaks, backslash escapes, and *reference-style* links --
``[label]: url``, not inline ``[text](url)``, which the spike confirmed
collapses to one opaque, uncaptured ``inline`` node on this grammar).
Reaching the inline grammar would mean hand-building a tree-sitter injection
subsystem Textual does not have (lode-ev5j.1's Q2) -- out of scope here.

**Graceful degradation.** ``textual[syntax]`` is a hard dependency
(``pyproject.toml``), but a broken/incomplete environment (the grammar
package missing or failing to build) must not kill the screen -- Textual
raises :class:`~textual.widgets.text_area.LanguageDoesNotExist` from inside
``TextArea.__init__`` itself when the ``language`` kwarg names a grammar it
can't resolve. Catch it here and fall back to a plain, uncoloured
``TextArea`` with everything else (text, read-only, id) unchanged, so editing
and scrolling keep working.
"""

from __future__ import annotations

from textual.widgets import TextArea
from textual.widgets.text_area import LanguageDoesNotExist

#: Passed to ``TextArea(language=...)`` -- Textual's bundled block-grammar
#: query for this name is ``markdown.scm`` (see this module's docstring for
#: exactly which tokens it captures).
_LANGUAGE = "markdown"


def markdown_text_area(text: str = "", *, id: str, read_only: bool = False) -> TextArea:
    """A note-body ``TextArea`` with markdown colouring, or plain text if unavailable.

    Args:
        text: Initial buffer content (defaults to empty -- all three callers
            load the real body later, in ``on_mount``).
        id: The widget id the caller's tests key off of.
        read_only: ``False`` for :class:`~lode.tui.screens.edit.EditScreen`'s
            editable body; ``True`` for the two read-only viewers.
    """
    try:
        return TextArea(text, language=_LANGUAGE, read_only=read_only, id=id)
    except LanguageDoesNotExist:
        return TextArea(text, read_only=read_only, id=id)

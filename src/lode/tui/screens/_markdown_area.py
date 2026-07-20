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

:class:`~lode.tui.screens.capture.CaptureScreen`'s body ``TextArea``
(``BODY_ID``) is **not** a caller either, but for a different reason: it is
simply outside lode-ev5j.2's settled three-screen scope, which named an
exclusion only for ``ReconcileScreen``. That leaves the one screen where a
user actually *types* markdown uncoloured while the editor colours it -- a
visible inconsistency tracked as **lode-ngk2**, not a reasoned design
exclusion like the diff view above.

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
package missing or failing to build) must not kill the screen. Two distinct
exceptions can come out of ``TextArea.__init__`` here, and *both* must be
caught -- catching only the first leaves the more likely failure uncovered:

* :class:`~textual.widgets.text_area.LanguageDoesNotExist` -- Textual's own
  signal that it could not resolve the grammar. It covers the cases
  ``textual._tree_sitter.get_language`` handles internally by returning
  ``None``: the ``tree_sitter_markdown`` package missing outright
  (``ImportError``) or failing to load (``OSError``/``AttributeError``).
* :class:`ValueError` -- raised by ``tree_sitter.Language()`` itself (e.g.
  ``invalid language ID``) when the grammar's compiled ABI and the installed
  ``tree-sitter`` core disagree. ``get_language`` does *not* catch this, so it
  propagates straight through ``TextArea.__init__`` uncaught. Because this
  repo deliberately leaves deps **unpinned** (see ``pyproject.toml``), an
  independently-resolved ``tree-sitter`` / ``tree-sitter-markdown`` pair is
  exactly how a real environment breaks -- making this the *more* probable
  path of the two, not an exotic one.

``ValueError`` is narrow enough to be safe here: Textual's own construction
bug for a malformed widget id is ``textual.dom.BadIdentifier``, which derives
directly from ``Exception`` and is **not** a ``ValueError``, so a genuine
programming error still surfaces rather than being swallowed.

Either way, fall back to a plain, uncoloured ``TextArea`` with everything else
(text, read-only, id) unchanged, so editing and scrolling keep working.
"""

from __future__ import annotations

from textual.widgets import TextArea
from textual.widgets.text_area import LanguageDoesNotExist


def _markdown_text_area(
    text: str = "", *, id: str, read_only: bool = False
) -> TextArea:
    """A note-body ``TextArea`` with markdown colouring, or plain text if unavailable.

    Args:
        text: Initial buffer content (defaults to empty -- all three callers
            load the real body later, in ``on_mount``).
        id: The widget id the caller's tests key off of.
        read_only: ``False`` for :class:`~lode.tui.screens.edit.EditScreen`'s
            editable body; ``True`` for the two read-only viewers.
    """
    try:
        return TextArea(text, language="markdown", read_only=read_only, id=id)
    except LanguageDoesNotExist, ValueError:
        # Both arms mean "no usable markdown grammar in this environment" --
        # see this module's docstring for why ValueError is required here and
        # why it is narrow enough not to mask a real bug.
        return TextArea(text, read_only=read_only, id=id)

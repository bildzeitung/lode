"""Construct a note-body ``TextArea`` with live markdown syntax colouring (lode-ev5j.2).

Shared by the four screens that show a note body as markdown --
:class:`~lode.tui.screens.capture.CaptureScreen` (``BODY_ID``, editable,
lode-ngk2), :class:`~lode.tui.screens.edit.EditScreen` (``EDIT_BODY_ID``,
editable), :class:`~lode.tui.screens.version_view.VersionViewScreen`
(``VERSION_BODY_ID``, read-only), and
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen`
(``SNAPSHOT_VIEWER_BODY_ID``, read-only) -- so the graceful-degradation
try/except lives in exactly one place rather than four copies (the ticket's
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
(text, read-only, id, placeholder) unchanged, so editing and scrolling keep
working.
"""

from __future__ import annotations

from rich.style import Style
from textual.widgets import TextArea
from textual.widgets.text_area import LanguageDoesNotExist, TextAreaTheme

#: Semantic declaration of the note-body markdown palette (lode-lab1,
#: retuned lode-dmbc), kept as a plain module-level dict -- same convention as
#: ``CLI_STYLES`` in ``lode.cli`` -- so a test can assert OUR palette rather
#: than the library default. (``TextAreaTheme`` is a ``@dataclass`` that keeps
#: ``syntax_styles`` as a plain attribute, so unlike rich's ``Theme`` it would
#: not destroy an inlined declaration -- the dict is hoisted for the test seam,
#: not to dodge a constructor.)
#:
#: Keys are tree-sitter capture names emitted by Textual's *bundled* markdown
#: grammar (``textual/tree-sitter/highlights/markdown.scm``); that file is the
#: complete menu, and it is block-only -- there is no inline capture to map, so
#: bold/italic/inline-code/inline-link colouring is unreachable here (see this
#: module's docstring and lode-ev5j.1).
#:
#: MAINTAINER DECISION (lode-lab1 notes, revised lode-dmbc): colour only -- no
#: bold, no background tint. Every value sits in the 256-colour range rather
#: than the standard 16: indices 0-15 are remapped by the user's terminal
#: theme, which is what made the original ``"magenta"`` (index 5) read as harsh
#: and gave no way to soften it. The 256-colour names render consistently
#: without requiring truecolor, so hex was not needed.
#:
#: * ``text.literal`` -- lode-76go's spike confirmed this carries a whole-line
#:   span on every line of a fenced code block (delimiters, info string, and
#:   body alike), and it also covers indented code blocks and reference-link
#:   titles. ``plum3`` (176) is the muted mauve that replaced ``magenta``.
#: * ``punctuation.delimiter`` -- the ``` fence lines themselves. Dimmed so the
#:   fence recedes behind the code it wraps. Rendered *over* the ``text.literal``
#:   whole-line span on those two lines, which is exactly the intent.
#: * ``heading.marker`` -- the ``#``..``######`` markers and the setext
#:   underlines. Present without competing with the heading text.
#: * ``list.marker`` -- bullet and ordered markers. NOTE: the grammar puts
#:   ``thematic_break`` (``---``) in this same capture, so horizontal rules take
#:   the bullet colour; there is no way to separate them without a custom
#:   highlight query.
#:
#: Deliberately does **not** map ``"none"``: lode-76go found that capture
#: appears later in each line's highlight iteration order, so mapping it would
#: emit a second, LATER Rich span that wins the colour attribute at render time
#: and silently overrides ``text.literal`` again.
NOTE_BODY_SYNTAX_STYLES: dict[str, Style] = {
    "text.literal": Style(color="plum3"),
    "punctuation.delimiter": Style(color="grey42"),
    "heading.marker": Style(color="steel_blue3"),
    "list.marker": Style(color="dark_sea_green4"),
}

#: The one shared ``TextAreaTheme`` for the TUI's note-body screens (lode-lab1)
#: -- a PARALLEL mechanism to ``CLI_STYLES``/``CLI_THEME`` in ``lode.cli``, not
#: an extension of it: that one is a rich ``Theme`` for CLI output, this one is
#: a ``textual`` ``TextAreaTheme`` keyed by tree-sitter capture names, and the
#: two share no code path. Registered on, and applied to, every ``TextArea``
#: this module builds with a working markdown grammar (see
#: ``_markdown_text_area`` below); left off the graceful-degradation fallback,
#: which has no grammar to colour in the first place.
NOTE_BODY_THEME = TextAreaTheme(
    name="lode-note-body", syntax_styles=NOTE_BODY_SYNTAX_STYLES
)


def _markdown_text_area(
    text: str = "", *, id: str, read_only: bool = False, placeholder: str = ""
) -> TextArea:
    """A note-body ``TextArea`` with markdown colouring, or plain text if unavailable.

    Args:
        text: Initial buffer content (defaults to empty -- the editor and the
            two viewers load the real body later, in ``on_mount``, and
            ``CaptureScreen`` starts empty by design).
        id: The widget id the caller's tests key off of.
        read_only: ``False`` for an editable body, ``True`` for a read-only
            viewer.
        placeholder: Prompt shown while the buffer is empty (Textual renders it
            only then). Plumbed through rather than left to the caller so a
            body stays fully declared at its ``compose`` site, the way every
            other placeholder in this TUI is.
    """
    try:
        text_area = TextArea(
            text,
            language="markdown",
            read_only=read_only,
            id=id,
            placeholder=placeholder,
        )
    except LanguageDoesNotExist, ValueError:
        # Both arms mean "no usable markdown grammar in this environment" --
        # see this module's docstring for why ValueError is required here and
        # why it is narrow enough not to mask a real bug.
        return TextArea(text, read_only=read_only, id=id, placeholder=placeholder)

    # Deliberately OUTSIDE the try: the ``except`` above is scoped to
    # ``TextArea.__init__``'s two documented grammar failures and nothing else.
    # Covering these two lines with it would let an unrelated ``ValueError``
    # from theme application silently degrade to the uncoloured fallback --
    # exactly the third failure mode the ticket forbids.
    text_area.register_theme(NOTE_BODY_THEME)
    text_area.theme = NOTE_BODY_THEME.name
    return text_area

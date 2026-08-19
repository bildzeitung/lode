"""Pure ``Text``-rendering helpers shared across the browse-family screens (lode-s5kp.4).

Promoted out of ``browse.py`` into this leaf module because these 7 functions
are pure formatting -- no widget, no navigation, no DB access beyond the args
already passed in -- and are reused by several browse-family screens
(:class:`~lode.tui.screens.version_view.VersionViewScreen`,
:class:`~lode.tui.screens.enrichment_modal.EnrichmentModalScreen`,
:class:`~lode.tui.screens.external_picker.ExternalPickerScreen`,
:class:`~lode.tui.screens.browse.BrowseScreen`). Giving them their own home
removed the shared-helper-home ambiguity the later screen-extraction ticket
(lode-s5kp.1, since done -- those screens now each live in their own module
too) would otherwise have had to settle on its own.

**Deliberately imports no ``Screen``/``Widget`` subclass** -- that is the
whole point of putting these here rather than leaving them in ``browse.py``:
a leaf module any browse-family screen can import from without introducing a
cycle. ``_view_note_external_content`` and ``_resolve_externals`` never
joined this module, on purpose: the former pushes
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` and
:class:`~lode.tui.screens.external_picker.ExternalPickerScreen` directly, so
it is navigation glue, not a pure render helper, and moving it here would
pull those two screens into this otherwise Screen-free module. They instead
got their own leaf module, :mod:`lode.tui.screens._content_view`
(lode-2zj0).

Pure move -- no behavior change; ``docs/conventions.md``'s underscore-prefix
module naming marks this as not itself a screen, so it doesn't count against
that file's one-Screen/Widget-per-module fiat.
"""

from __future__ import annotations

import textwrap

from rich.text import Text

from lode.enrichment_view import EnrichmentEdge, EnrichmentItem, ExternalView
from lode.ids import short_version_id
from lode.notes_read import short_note_id

#: Placeholder text for an empty section -- never suppressed, just labeled.
_NONE_TEXT = "(none)"

#: Fixed row height -- every row is capped to this many lines, with overflow
#: ellipsized instead of wrapped, rather than growing the row (and so the
#: whole list) as tall as a long summary needs via ``height=None``.
#: Summaries are prompted lede-first so the single visible line still
#: carries the note's point.
_SUMMARY_ROW_HEIGHT = 1


def _wrap_summary_full(summary: str, width: int) -> tuple[Text, int]:
    """Wrap *summary* to *width* with no line cap -- the full untruncated text.

    Companion to :func:`_clip_summary_to_row_height`, used for the one
    highlighted row a user has expanded (lode-juz8.4) instead of the
    1-line-capped rendering every other row gets. Returns the wrapped text
    and its line count together since the caller needs both in the same
    ``add_row`` call: the cell content and the row's ``height=``.

    Returns a :class:`~rich.text.Text`, not a ``str`` (lode-ix4i) -- a note
    summary is arbitrary user text, and a ``str`` cell in a ``DataTable`` is
    rendered through Rich console *markup*, which silently eats a bracketed
    substring like ``[draft]`` the same way ``str`` cells did in
    :class:`~lode.tui.screens.tags.TagsScreen` before lode-7abi. ``Text`` is
    never markup-parsed.
    """
    lines = textwrap.wrap(summary, width=width) or [""]
    return Text("\n".join(lines)), len(lines)


def _clip_summary_to_row_height(summary: str, width: int) -> Text:
    """Wrap *summary* to *width* and cap it at :data:`_SUMMARY_ROW_HEIGHT` lines.

    A ``DataTable`` row given a fixed ``height`` doesn't ellipsize overflow on
    its own -- it just clips whatever doesn't fit, mid-word, with no visual
    cue that anything is missing. So the wrapping is done here instead: the
    text is pre-wrapped to *width* and, if that produces more than
    :data:`_SUMMARY_ROW_HEIGHT` lines, the last visible line is truncated and
    given a trailing ellipsis so the cut is visible rather than silent.

    Returns a :class:`~rich.text.Text`, not a ``str`` -- see
    :func:`_wrap_summary_full`'s docstring for why.
    """
    if width <= 0:
        lines = [summary]
    else:
        lines = textwrap.wrap(summary, width=width) or [""]
        if len(lines) > _SUMMARY_ROW_HEIGHT:
            lines = lines[:_SUMMARY_ROW_HEIGHT]
            last = lines[-1][: max(width - 1, 0)].rstrip()
            lines[-1] = f"{last}\N{HORIZONTAL ELLIPSIS}"
    return Text("\n".join(lines))


def _item_text(item: EnrichmentItem) -> Text:
    """One tag/entity/summary value, dimmed if stale, italicized if inherited.

    Styles the ``stale``/``inherited`` bits directly rather than printing a
    baked-in suffix (lode-0qc, lode-f0m1) -- the whole reason
    :class:`~lode.enrichment_view.EnrichmentItem` carries both as structured
    flags instead of a suffixed string is so a consumer that wants to
    *style* an item (as this modal does) never has to string-sniff for a
    marker. ``inherited`` (a tag resolved through a linked external rather
    than scoped to this note directly) gets its own distinct style so it
    reads as "not this note's own tag" without merging into the ``stale``
    styling -- a tag can be inherited and fresh, inherited and stale, or
    neither, and all three must stay visually distinguishable.
    """
    style_parts = []
    if item.stale:
        style_parts.append("dim")
    if item.inherited:
        style_parts.append("italic")
    return Text(item.value, style=" ".join(style_parts))


def _items_line(items: list[EnrichmentItem]) -> Text:
    """Every tag/entity on one comma-separated line, each styled by its own bit."""
    if not items:
        return Text(_NONE_TEXT)
    line = Text()
    for index, item in enumerate(items):
        if index:
            line.append(", ")
        line.append_text(_item_text(item))
    return line


def _summary_text(summary: EnrichmentItem | None) -> Text:
    """The note's one summary line, or the placeholder when it has none at all."""
    if summary is None:
        return Text(_NONE_TEXT)
    return _item_text(summary)


def _external_text(external: ExternalView) -> Text:
    """One edge's external-snapshot introspection sub-line (lode-8d2).

    Rendered directly beneath its edge's own line in the same Edges block --
    the external analogue of a note's tags/entities, through the exact
    :class:`~lode.enrichment_view.ExternalView` fields
    :func:`~lode.enrichment_view.enrichment_view` already assembled (no
    second DB read, no re-derived policy). Dimmed for ``stale``/``withheld``
    the same way a stale tag/edge already is (lode-0qc); the default
    ``un-refreshed`` state renders plain, but is still printed explicitly so
    all three states are equally visible in the modal.
    """
    line = (
        f"     {external.source_type} · snapshot "
        f"{short_version_id(external.snapshot_id)} · as of {external.fetched_at} "
        f"[{external.state}]"
    )
    return Text(line, style="dim" if external.state != "un-refreshed" else "")


def _edges_text(edges: list[EnrichmentEdge]) -> Text:
    """One inferred edge per line: target, reason, confidence -- dimmed if stale.

    ``reason``/``confidence`` are nullable on the seam (a user-curated edge may
    carry neither); missing values render as an explicit placeholder rather
    than a blank so the line never reads as truncated. When an edge draws down
    an external (``edge.external`` is not ``None``, lode-8d2), the target
    label is the bare ``to_id`` (the source URL) rather than the truncated
    :func:`~lode.notes_read.short_note_id` prefix -- that prefix is an
    8-char slice meant for note ids, not a URL -- and a second, indented line
    shows that external's snapshot introspection (:func:`_external_text`).
    """
    if not edges:
        return Text(_NONE_TEXT)
    block = Text()
    for index, edge in enumerate(edges):
        if index:
            block.append("\n")
        reason = edge.reason if edge.reason is not None else "no reason recorded"
        confidence = f"{edge.confidence:.2f}" if edge.confidence is not None else "n/a"
        target = edge.to_id if edge.external is not None else short_note_id(edge.to_id)
        line = f"-> {target} ({reason}, {confidence})"
        block.append(line, style="dim" if edge.stale else "")
        if edge.external is not None:
            block.append("\n")
            block.append_text(_external_text(edge.external))
    return block

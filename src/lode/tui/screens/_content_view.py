"""Content-viewer navigation glue shared by browse and edit (lode-2zj0).

Extracted from :mod:`lode.tui.screens.browse`. ``_resolve_externals`` and
``_view_note_external_content`` implement the shared zero/one/many
addressing rule both :meth:`~lode.tui.screens.browse.BrowseScreen.action_view_content` and :meth:`~lode.tui.screens.edit.EditScreen.action_view_content` push through (lode-0sjj) -- mirroring ``lode
dump-html``'s CLI disambiguation (lode-olmi.7) on purpose, so the CLI and TUI
can't drift onto two different rules for the same question: zero externals
notifies ``'no retrieved content for this note'``; exactly one pushes
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` directly;
more than one pushes :class:`~lode.tui.screens.external_picker.ExternalPickerScreen` first, which pushes the viewer itself once a row is
chosen.

**Why a leaf module, not left in ``browse.py`` (lode-s5kp.1's original
call).** Before this, ``browse.py`` imported :class:`~lode.tui.screens.edit.EditScreen` (to push it on row-select) while ``edit.py`` needed to reach
back into ``browse._view_note_external_content`` -- a cycle, broken only by
a method-local import inside :meth:`~lode.tui.screens.edit.EditScreen.action_view_content`. Both functions are leaf-eligible: a generic
``Screen[None]`` signature, and they depend only on
:func:`~lode.enrichment_view.enrichment_view`,
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen`, and
:class:`~lode.tui.screens.external_picker.ExternalPickerScreen` -- none of
which reach back into ``browse``/``edit``. Hosting them here instead lets
both ``browse.py`` and ``edit.py`` import at module level, dissolving the
cycle and removing the lazy import entirely.

**Not folded into :mod:`lode.tui.screens._browse_render`** (the sibling leaf
module of pure render helpers, lode-s5kp.4): that module deliberately stays
Screen-free (its own docstring), and these two functions push
``SnapshotViewerScreen``/``ExternalPickerScreen`` directly, so they are
navigation glue, not a pure render helper -- the same distinction that kept
them out of ``_browse_render`` when it was created.

Pure move -- no behavior change; ``docs/conventions.md``'s underscore-prefix
module naming marks this as not itself a screen, so it doesn't count against
that file's one-Screen/Widget-per-module fiat.
"""

from __future__ import annotations

from pathlib import Path

from textual.screen import Screen

from lode.enrichment_view import ExternalView, enrichment_view
from lode.tui.screens.external_picker import ExternalPickerScreen
from lode.tui.screens.snapshot_viewer import SnapshotViewerScreen


def _resolve_externals(db_path: Path, note_id: str) -> list[ExternalView]:
    """*note_id*'s drawn-down external edges, in edge order (lode-0sjj).

    The one place the "which externals does this note have" question is
    answered for the content-viewer feature -- both
    :meth:`~lode.tui.screens.browse.BrowseScreen.action_view_content` and
    :meth:`~lode.tui.screens.edit.EditScreen.action_view_content` resolve
    through this (via :func:`_view_note_external_content`) rather than each
    independently filtering :func:`~lode.enrichment_view.enrichment_view`'s
    edges, which would risk the two screens silently drifting onto different
    rules. A missing note (should never happen -- both callers only ever have
    a real, live ``note_id`` in hand) returns an empty list rather than
    raising; an empty result and "note exists but has no external edges" are
    indistinguishable to the caller, which is fine -- both mean "notify,
    don't view anything."
    """
    view = enrichment_view(db_path, note_id)
    if view is None:
        return []
    return [edge.external for edge in view.edges if edge.external is not None]


def _view_note_external_content(screen: Screen[None], note_id: str) -> None:
    """Resolve *note_id*'s externals and push the right viewer (lode-0sjj).

    Shared by :meth:`~lode.tui.screens.browse.BrowseScreen.action_view_content` (bare ``v``) and :meth:`~lode.tui.screens.edit.EditScreen.action_view_content` (a Ctrl-prefixed key) so the zero/one/many
    addressing rule lives in exactly one place -- mirroring ``lode
    dump-html``'s CLI disambiguation (lode-olmi.7) on purpose. Zero externals
    notifies cleanly; exactly one pushes :class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` directly; more than one pushes
    :class:`~lode.tui.screens.external_picker.ExternalPickerScreen` first,
    which pushes the viewer itself once a row is chosen.
    """
    externals = _resolve_externals(screen.app.db_path, note_id)
    if not externals:
        screen.notify("no retrieved content for this note", severity="warning")
    elif len(externals) == 1:
        screen.app.push_screen(SnapshotViewerScreen(externals[0].snapshot_id))
    else:
        screen.app.push_screen(ExternalPickerScreen(externals))

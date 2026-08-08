"""A glance-and-dismiss popup over one note's full enrichment (lode-ay5.2, extracted lode-s5kp.1).

Split out of :mod:`lode.tui.screens.browse` per the one-Screen-per-module fiat
(``docs/conventions.md``). Pushed from
:meth:`~lode.tui.screens.browse.BrowseScreen.action_inspect_selected` via
``i`` on the highlighted row, and from :meth:`~lode.tui.screens.edit.EditScreen.action_inspect_selected` via ``Ctrl+G``, both keyed to a
``note_id``.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen

from lode.enrichment_view import enrichment_view
from lode.tui.screens._browse_render import _edges_text, _items_line, _summary_text
from lode.tui.widgets.lode_static import LodeStatic

#: The enrichment inspector modal's dialog container id -- read back in tests.
INSPECTOR_DIALOG_ID = "enrichment-inspector-dialog"
#: The inspector's ``enrichment_state`` line id -- read back in tests.
INSPECTOR_STATE_ID = "inspector-state"
#: The inspector's summary line id -- read back in tests.
INSPECTOR_SUMMARY_ID = "inspector-summary"
#: The inspector's tags line id -- read back in tests.
INSPECTOR_TAGS_ID = "inspector-tags"
#: The inspector's entities line id -- read back in tests.
INSPECTOR_ENTITIES_ID = "inspector-entities"
#: The inspector's inferred-edges block id -- read back in tests.
INSPECTOR_EDGES_ID = "inspector-edges"
#: The inspector's embed-status line id -- read back in tests.
INSPECTOR_EMBED_ID = "inspector-embed"


class EnrichmentModalScreen(ModalScreen[None]):
    """A glance-and-dismiss popup over one note's full enrichment (lode-ay5.2).

    Pushed from :meth:`~lode.tui.screens.browse.BrowseScreen.action_inspect_selected` via ``i`` on the highlighted row, and from
    :meth:`~lode.tui.screens.edit.EditScreen.action_inspect_selected` via
    ``Ctrl+G``, both keyed to a ``note_id``. Renders :func:`lode.enrichment_view.enrichment_view` verbatim -- summary, tags, entities,
    inferred edges (reason+confidence+stale), embed status, and the
    three-valued ``enrichment_state`` -- with **no** DB access or display
    policy of its own; this screen only shapes the already-decided fields
    into widgets. The ``_item_text``/``_items_line``/``_edges_text`` helpers
    in :mod:`~lode.tui.screens._browse_render` (lode-s5kp.4) do the one bit
    of real work this modal owns: styling ``stale`` dim instead of
    string-sniffing a suffix (lode-0qc; see ``docs/storage.md``'s
    "Enrichment view-model" section).

    Content lives in a :class:`~textual.containers.VerticalScroll` (not a
    fixed :class:`~textual.containers.Vertical`, unlike
    :class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`'s small
    fixed dialog) so a note with many tags/entities/edges scrolls within the
    popup rather than overflowing or truncating. ``Esc`` pops back to
    whichever screen pushed it -- the same one-level-at-a-time contract every
    other screen in this cluster already uses. Like ``DiscardConfirmScreen``,
    this is a bare ``ModalScreen`` pushed directly (not a :data:`~lode.tui.app.LodeApp.SCREENS` entry): it dims the screen underneath for free via
    ``ModalScreen``'s own ``DEFAULT_CSS``, and ``lode.tcss`` adds only sizing
    and centering for :data:`INSPECTOR_DIALOG_ID`.
    """

    # escape/Back uses the APP-NAMESPACED "app.pop_screen" -- the bare
    # "pop_screen" silently fails on a Screen. See docs/keybindings.md.
    BINDINGS: ClassVar = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, note_id: str) -> None:
        super().__init__()
        self.note_id = note_id

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            LodeStatic("", id=INSPECTOR_STATE_ID),
            LodeStatic("", id=INSPECTOR_SUMMARY_ID),
            LodeStatic("", id=INSPECTOR_TAGS_ID),
            LodeStatic("", id=INSPECTOR_ENTITIES_ID),
            LodeStatic("", id=INSPECTOR_EDGES_ID),
            LodeStatic("", id=INSPECTOR_EMBED_ID),
            id=INSPECTOR_DIALOG_ID,
        )

    def on_mount(self) -> None:
        view = enrichment_view(self.app.db_path, self.note_id)
        if view is None:
            # BrowseScreen only ever pushes this for a row already in the
            # (live-only) table, so a missing note here would be a real bug,
            # not a normal race worth a soft fallback -- the same stance
            # EditScreen.on_mount takes for a missing head.
            raise LookupError(f"no live note {self.note_id!r} to inspect")

        self.query_one(f"#{INSPECTOR_STATE_ID}", LodeStatic).update(
            f"Enrichment: {view.enrichment_state}"
        )
        self.query_one(f"#{INSPECTOR_SUMMARY_ID}", LodeStatic).update(
            Text("Summary: ") + _summary_text(view.summary)
        )
        self.query_one(f"#{INSPECTOR_TAGS_ID}", LodeStatic).update(
            Text("Tags: ") + _items_line(view.tags)
        )
        self.query_one(f"#{INSPECTOR_ENTITIES_ID}", LodeStatic).update(
            Text("Entities: ") + _items_line(view.entities)
        )
        self.query_one(f"#{INSPECTOR_EDGES_ID}", LodeStatic).update(
            Text("Edges:\n") + _edges_text(view.edges)
        )
        self.query_one(f"#{INSPECTOR_EMBED_ID}", LodeStatic).update(
            f"Embedded: {'yes' if view.embedded else 'no'} "
            f"({view.passage_count} passages)"
        )

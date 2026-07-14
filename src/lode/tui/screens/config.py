"""Config/diagnostics screen (lode-3r4) — the TUI half of ``lode config``.

Renders the exact same row set as the CLI's ``lode config`` command, via the
ONE shared row-builder (:func:`lode.config.config_lines`, lode-u5gh) — a
read-only surface reachable from the capture screen via the app-level ``F2``
binding (:meth:`~lode.tui.app.LodeApp.action_show_config`). Before lode-u5gh
this screen built its own, independently-scoped row list, which had already
drifted from the CLI's once (lode-ak6 added a model-cache row to the CLI by
hand; nothing connected the two lists so this screen missed it). Collapsing
onto one shared builder closes that gap structurally: a row added to
:func:`~lode.config.config_lines` reaches both surfaces.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from lode.config import config_lines

#: The resolved-paths readout widget id — read back in tests.
ROWS_ID = "config-rows"


class ConfigScreen(Screen[None]):
    """Read-only diagnostics view: the resolved paths ``lode config`` shows.

    Reads ``self.app.db_path`` (resolved once by :class:`~lode.tui.app.LodeApp`)
    and renders it through the shared :func:`~lode.config.config_lines` — no
    path derivation or row-building lives here. Escape pops back to whichever
    screen was active before.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("\n".join(config_lines(self.app.db_path)), id=ROWS_ID),
        )
        yield Footer()

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()

"""Config/diagnostics screen (lode-3r4) — the TUI half of ``lode config``.

A read-only surface reachable from the capture screen via the app-level ``F2``
binding (:meth:`~lode.tui.app.LodeApp.action_show_config`). It renders the exact
same rows as the CLI's ``lode config``, because both call the ONE shared
row-builder :func:`lode.config.config_lines` — which is where the rows, and why
they are shared rather than mirrored by hand, are documented (lode-u5gh).
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

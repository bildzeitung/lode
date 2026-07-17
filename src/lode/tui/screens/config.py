"""Config/diagnostics screen (lode-3r4) — the TUI half of ``lode config``.

A read-only surface reachable from the capture screen via the app-level
``Ctrl+O`` binding (:meth:`~lode.tui.app.LodeApp.action_show_config`). It renders the exact
same rows as the CLI's ``lode config``, because both call the ONE shared
row-builder :func:`lode.config.config_lines` for the resolved on-disk paths
(lode-u5gh) and the ONE shared row-builder :func:`lode.config.knob_rows` for
every runtime/tune Settings knob and its current value (lode-juz8.6) — where
the rows, and why they are shared rather than mirrored by hand, are
documented.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Header, Static

from lode.config import config_lines, knob_rows
from lode.tui.lode_footer import LodeFooter

#: The resolved-paths readout widget id — read back in tests.
ROWS_ID = "config-rows"

#: The runtime/tune knob table widget id — read back in tests.
KNOB_TABLE_ID = "config-knobs"


class ConfigScreen(Screen[None]):
    """Read-only diagnostics view: the resolved paths + knob table ``lode config`` shows.

    Reads ``self.app.db_path`` (resolved once by :class:`~lode.tui.app.LodeApp`)
    and renders it through the shared :func:`~lode.config.config_lines` — no
    path derivation or row-building lives here. Below that, ``self.app.settings``
    (also resolved once by ``LodeApp``) feeds the shared
    :func:`~lode.config.knob_rows` into a ``DataTable`` — the "TUI uses a table"
    half of lode-juz8.6's widened contract; the CLI renders the identical row
    data as a terminal-width-aware rich ``Table`` instead
    (:func:`lode.cli._config_knob_table`, lode-l38d.4). Escape pops back to
    whichever screen was active before.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("\n".join(config_lines(self.app.db_path)), id=ROWS_ID),
            DataTable(id=KNOB_TABLE_ID, cursor_type="row"),
        )
        yield LodeFooter()

    def on_mount(self) -> None:
        table = self.query_one(f"#{KNOB_TABLE_ID}", DataTable)
        table.add_columns("Knob", "Value", "Kind")
        table.add_rows(knob_rows(self.app.settings))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()

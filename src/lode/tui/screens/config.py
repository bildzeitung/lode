"""Config/diagnostics screen (lode-3r4) — the TUI half of ``lode config``.

The CLI's ``lode config`` command and the resolver functions it reads from
(``lode.config.config_path`` / ``lance_dir`` / ``log_dir`` / ``lode_home``)
already surface the single-root ``$LODE_HOME`` layout (``docs/configuration.md``).
This screen shows the *same* resolved paths — read from those resolvers, never
re-derived — as a thin, read-only surface reachable from the capture screen via
the app-level ``F2`` binding (:meth:`~lode.tui.app.LodeApp.action_show_config`).
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from lode.config import config_path, lance_dir, lode_home, log_dir

#: The resolved-paths readout widget id — read back in tests.
ROWS_ID = "config-rows"


def _config_lines(db_path: Path) -> list[str]:
    """Render the resolved on-disk paths as aligned ``label  value`` lines.

    Scoped to this ticket's acceptance set — ``$LODE_HOME``, the DB path (the
    app's already-resolved ``db_path``, not re-derived here), the vector
    store, the log dir, and the config file's present/absent state — mirroring
    ``lode.cli._config_lines``'s format without importing the Typer-only CLI
    module.
    """
    cfg = config_path()
    config_state = "present" if cfg.exists() else "absent"
    rows = [
        ("LODE_HOME", str(lode_home())),
        ("database", str(db_path)),
        ("vector store", str(lance_dir(db_path))),
        ("logs", str(log_dir())),
        ("config", f"{cfg}  ({config_state})"),
    ]
    width = max(len(label) for label, _ in rows)
    return [f"{label:<{width}}  {value}" for label, value in rows]


class ConfigScreen(Screen[None]):
    """Read-only diagnostics view: the resolved paths ``lode config`` shows.

    Reads ``self.app.db_path`` (resolved once by :class:`~lode.tui.app.LodeApp`)
    plus ``lode.config``'s resolvers directly — no path derivation lives here.
    Escape pops back to whichever screen was active before.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("\n".join(_config_lines(self.app.db_path)), id=ROWS_ID),
        )
        yield Footer()

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()

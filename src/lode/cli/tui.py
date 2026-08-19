"""``lode tui`` -- launch the Textual TUI, starting on the instant capture screen."""

import logging

import typer

from lode import cli
from lode.cli import _DbOption, app
from lode.config import default_db_path, log_dir


@app.command()
def tui(ctx: typer.Context, db: _DbOption = None) -> None:
    """Launch the Textual TUI, starting on the instant capture screen.

    Logs go to $LODE_HOME/logs/lode.log instead of the terminal, since the
    TUI takes over the screen. The top-level --debug flag still raises the
    log file's verbosity.
    """
    level = logging.DEBUG if ctx.obj.debug else None
    # Looked up through the package (`cli.configure_logging`, not a plain
    # imported name) because tests spy on this SECOND, file-only reconfigure
    # call via `monkeypatch.setattr(cli, "configure_logging", ...)` -- see
    # `lode.cli`'s own module docstring for why every such call site needs
    # this indirection.
    cli.configure_logging(level=level, log_dir=log_dir(), console=False)

    from lode.tui.app import run as run_tui

    # Resolved once here and threaded onto LodeApp (lode-40g) -- every screen
    # then reads it back via self.app.settings (lode.tui.app's single
    # resolve-once-and-share pattern), rather than each screen falling back to
    # its own bare Settings() default independently.
    run_tui(db_path=db or default_db_path(), settings=cli._resolve_settings())

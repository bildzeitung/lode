"""``lode backfill`` -- re-run a connector's draw-down under CURRENT routing."""

from typing import Annotated

import typer

from lode import cli
from lode.cli import _DbOption, _open_db, app


@app.command(
    help=(
        "Re-run a connector's draw-down under current routing.\n\n"
        "Run this when links you already captured should now resolve "
        "differently -- typically after a connector that handles those URLs "
        "became available. Pass no CONNECTOR to see the registered names, "
        "and try --dry-run before a real pass."
    )
)
def backfill(
    connector: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Registered connector name (see 'lode backfill --list'). Omit to "
                "list registered connectors."
            )
        ),
    ] = None,
    list_connectors: Annotated[
        bool,
        typer.Option(
            "--list",
            help="List registered connectors and exit.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report what would change without writing anything.",
        ),
    ] = False,
    retry_tombstoned: Annotated[
        bool,
        typer.Option(
            "--retry-tombstoned",
            help=(
                "Also retry a target whose head snapshot already tombstoned "
                "on a prior backfill pass. Not needed on a first migration."
            ),
        ),
    ] = False,
    db: _DbOption = None,
) -> None:
    """Re-run a connector's draw-down for its already-processed links under CURRENT routing.

    CLI only -- no TUI surface. Runs per-machine, wherever this DB lives
    ($LODE_HOME); it does not travel on the Dolt/git wire.

    A "connector" here is whatever has registered itself into the backfill
    framework's registry (lode.backfill.register_backfill) -- this command
    is just the dispatcher; the framework itself ships no connector logic of
    its own (see 'jira', lode-gpzn.10, and 'confluence', lode-gpzn.11 --
    both built in). 'confluence' registers itself explicitly on every
    invocation of this command rather than relying on a bare module-level
    register_backfill(...) import-time side effect (see
    lode.confluence_backfill.register's own docstring for why). With no
    CONNECTOR argument (or --list), the registered names are printed instead
    of running anything.

    --dry-run reports what the connector's handler would change without
    writing. --retry-tombstoned is the explicit, human-driven opt-in to also
    retry a target that already permanently failed (tombstoned) on a prior
    backfill pass -- see the command's own help on that flag.
    """
    from lode.backfill import BackfillError, registered_backfills, run_backfill

    # Built-in connectors register themselves here, explicitly, on every
    # invocation -- see lode.confluence_backfill.register's own docstring
    # for why this must be a function call rather than relying on a
    # connector module's import to trigger a bare module-level
    # register_backfill(...) side effect.
    from lode.confluence_backfill import register as _register_confluence

    _register_confluence()

    if list_connectors or connector is None:
        names = registered_backfills()
        if not names:
            typer.echo("no connectors registered for backfill")
        else:
            for name in names:
                typer.echo(name)
        return

    settings = cli._resolve_settings()
    conn = _open_db(db)
    try:
        try:
            summary = run_backfill(
                conn,
                settings,
                connector,
                dry_run=dry_run,
                retry_tombstoned=retry_tombstoned,
            )
        except BackfillError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
        typer.echo(summary)
    finally:
        conn.close()

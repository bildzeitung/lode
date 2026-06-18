"""lode command-line entry point.

This is the minimal scaffolding skeleton: a Typer app wired to the ``lode``
console-script so ``lode --help`` lists subcommands. The full subcommand
surface (add / ask / purge / status / eval) is built in later E0/E10 tasks.
"""

import typer

from lode import __version__

app = typer.Typer(
    name="lode",
    help="AI-first personal knowledge base for things you learn at work.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """lode — capture and retrieve what you learn at work."""
    # Group callback: keeps lode a multi-command app so ``--help`` lists
    # subcommands even while only ``version`` exists. Real subcommands
    # (add / ask / purge / status / eval) land in later E0/E10 tasks.


@app.command()
def version() -> None:
    """Print the installed lode version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover
    app()

"""lode command-line entry point.

This is the scaffolding skeleton: a Typer app wired to the ``lode``
console-script so ``lode --help`` lists the subcommand surface. The five
subcommands (add / ask / purge / status / eval) exist here as dispatching
stubs; their real behaviour is built in later E0/E10 tasks.
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
    # Group callback: keeps lode a multi-command app so ``--help`` lists the
    # subcommands. Real behaviour for add / ask / purge / status / eval lands
    # in later E0/E10 tasks.


def _stub(command: str) -> None:
    """Placeholder body for a subcommand whose real behaviour lands in E10."""
    typer.echo(f"lode {command}: not yet implemented (lands in E10).")


@app.command()
def add() -> None:
    """Capture a note into lode (stub; lands in E10)."""
    _stub("add")


@app.command()
def ask() -> None:
    """Ask a cited question over the corpus (stub; lands in E10)."""
    _stub("ask")


@app.command()
def purge() -> None:
    """Hard-delete notes and their derived data (stub; lands in E10)."""
    _stub("purge")


@app.command()
def status() -> None:
    """Show store and work-queue status (stub; lands in E10)."""
    _stub("status")


@app.command(name="eval")
def eval_() -> None:
    """Run the eval harness over the golden set (stub; lands in E10)."""
    _stub("eval")


@app.command()
def version() -> None:
    """Print the installed lode version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover
    app()

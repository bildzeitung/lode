"""``lode version`` -- print the installed lode version."""

import typer

from lode import __version__
from lode.cli import app


@app.command()
def version() -> None:
    """Print the installed lode version."""
    typer.echo(__version__)

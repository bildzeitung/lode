"""``lode version`` -- print lode's wordmark and the installed version.

The wordmark (lode-fhql.5) prints unstyled, straight through ``typer.echo``
rather than the shared rich ``console`` -- there is no colour in it to gate
on NO_COLOR/TTY (see :mod:`lode.branding`'s module docstring), and
``typer.echo`` already writes cleanly to a pipe with no escape codes either
way. :func:`lode.branding.wordmark` picks Unicode vs. ASCII glyphs from
``sys.stdout``'s own encoding, explicitly, before anything is printed.
"""

import typer

from lode import __version__
from lode.branding import wordmark
from lode.cli import app


@app.command()
def version() -> None:
    """Print lode's wordmark and the installed version."""
    typer.echo(wordmark())
    typer.echo(__version__)

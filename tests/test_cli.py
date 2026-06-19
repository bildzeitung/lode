"""Smoke tests for the lode CLI skeleton.

The initial suite exists so ``nox -s tests`` has something to run and exits 0
(pytest exits non-zero when it collects no tests). These assert the Typer app
is wired to the ``lode`` entry point; the real subcommand behaviour lands in
later E0/E10 tasks.
"""

from typer.testing import CliRunner

from lode import __version__
from lode.cli import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_usage() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout

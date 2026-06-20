"""Smoke tests for the lode CLI skeleton.

Asserts the lode-txh.5 acceptance criteria: the five subcommands
(add / ask / purge / status / eval) exist and dispatch (stubbed), and
``lode --help`` lists all five. The real subcommand behaviour lands in later
E0/E10 tasks.
"""

import pytest
from typer.testing import CliRunner

from lode import __version__
from lode.cli import app

runner = CliRunner()

STUB_SUBCOMMANDS = ["add", "ask", "purge", "status", "eval"]


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_usage() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_help_lists_all_five_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in STUB_SUBCOMMANDS:
        assert name in result.stdout


@pytest.mark.parametrize("name", STUB_SUBCOMMANDS)
def test_subcommand_dispatches(name: str) -> None:
    result = runner.invoke(app, [name])
    assert result.exit_code == 0
    assert name in result.stdout

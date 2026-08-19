"""Tests for ``main()``'s global ``[cli.theme]`` resolution + application
(lode-mk9j) -- the placement decision recorded in ``docs/decisions.md``'s
``lode-mk9j`` entry, 2026-08-18.

Covers: an override actually restyles the shared ``console``/``err_console``
for a command that never itself resolves settings (``lode notes``, the
concrete acceptance-criteria example); absent ``[cli.theme]`` leaves the
defaults byte-identical; an invalid ``[cli.theme]`` value fails at config
load, naming the key, on a command that never read config before this
ticket; and ``lode status`` alone survives a broken config, per its
pre-existing ``lode-l38d.6`` contract (unchanged, re-asserted here as a
regression guard against this ticket's own global resolution).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.style import Style
from typer.testing import CliRunner

from lode.cli import CLI_STYLES, app, console, err_console
from lode.storage import init_db

runner = CliRunner()


def test_notes_command_applies_a_configured_style_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[cli.theme.styles]\nnote_id = "bold magenta"\n', encoding="utf-8"
    )
    monkeypatch.setenv("LODE_HOME", str(home))

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["notes", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    # `lode notes` never calls `_resolve_settings()` itself (lode-mk9j's own
    # motivating gap) -- so seeing the override applied proves main()'s
    # global resolution reached it.
    assert console.get_style("note_id") == Style.parse("bold magenta")
    assert err_console.get_style("note_id") == Style.parse("bold magenta")


def test_absent_cli_theme_leaves_defaults_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["notes", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    for name, spec in CLI_STYLES.items():
        assert console.get_style(name) == Style.parse(spec)


def test_invalid_cli_theme_value_fails_loudly_on_a_command_that_never_read_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The maintainer's accepted side effect: `lode notes` never called
    # _resolve_settings() before this ticket, so a bad [cli.theme] value
    # (or any other config error) now takes it down too -- an improvement
    # (a stale/typo'd config surfaces immediately), not a regression.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[cli.theme.styles]\nnote_id = "not a real style xyz"\n', encoding="utf-8"
    )
    monkeypatch.setenv("LODE_HOME", str(home))

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["notes", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "cli.theme.styles.note_id" in result.output


def test_status_still_survives_a_malformed_config_despite_global_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for the exact failure mode the maintainer's decision
    # exists to prevent: a first attempt at resolving [cli.theme] globally
    # in main() broke `lode status`'s pre-existing lode-l38d.6 survival
    # contract (tests/test_cli.py's own malformed/unreadable variants) by
    # taking the WHOLE command down before its body ever ran. This is the
    # same scenario, asserted from this ticket's own test module too.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("embedding_model = [not valid toml\n")
    monkeypatch.setenv("LODE_HOME", str(home))

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "No action needed." in result.output

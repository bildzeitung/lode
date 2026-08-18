"""Tests for ``lode theme export`` (lode-cwyk).

The escape hatch ``lode-5zxt``'s settled design promises: print the fully-
resolved effective ``[tui.theme]`` as ready-to-paste TOML. The load-bearing
property is the **round trip**: pasting the output back into ``config.toml``
must reproduce the same effective theme -- covered end to end here by
actually writing the exported TOML to a real ``config.toml`` and reloading it
through :func:`lode.config.load_settings`, not just by inspecting the printed
text.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lode.cli import app as cli_app
from lode.config import load_settings
from lode.theming import resolve_note_body_theme, resolve_theme

runner = CliRunner()


def _run_export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> str:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    result = runner.invoke(cli_app, ["theme", "export", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_export_prints_valid_toml_with_every_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lode.config import TUI_THEME_COLOR_KEYS, TUI_THEME_SYNTAX_KEYS

    output = _run_export(monkeypatch, tmp_path)
    parsed = tomllib.loads(output)
    theme = parsed["tui"]["theme"]
    assert theme["name"] == "textual-dark"
    for key in TUI_THEME_COLOR_KEYS:
        assert key in theme["colors"]
    for key in TUI_THEME_SYNTAX_KEYS:
        assert key in theme["syntax"]


def test_export_default_round_trips_to_the_same_effective_theme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = _run_export(monkeypatch, tmp_path)
    before = resolve_theme(load_settings())
    before_syntax = resolve_note_body_theme(load_settings())

    (tmp_path / "config.toml").write_text(output, encoding="utf-8")
    after = resolve_theme(load_settings())
    after_syntax = resolve_note_body_theme(load_settings())

    assert after.name == before.name

    # Compare the EFFECTIVE (rendered) colour, not the raw dataclass field
    # OR a blind `.to_color_system().generate()` call on both sides:
    # background/surface/panel/boost are None on the (unmodified) base theme
    # and only get a literal value once exported, so the raw fields
    # legitimately differ (None vs. an explicit hex) even though the
    # rendered colour is identical -- and Textual's own `generate()` has a
    # quirk where an explicit `panel` silences its `boost` derivation
    # entirely (verified against textual 8.2.8's design.py), so comparing
    # `generate()` output on the POST-export theme (where panel/boost are
    # BOTH now literal) against the PRE-export theme (where both are still
    # None) would fail even though the app renders the identical colour
    # either way. Prefer the literal field (what round-tripping actually
    # preserves) and fall back to `generate()` only when it's unset --
    # exactly what `lode theme export` itself does (lode/cli/theme.py).
    def _effective(theme: object, field: str) -> str:
        literal = getattr(theme, field)
        if literal is not None:
            return str(literal).lower()
        return theme.to_color_system().generate()[field].lower()  # type: ignore[attr-defined]

    for field in (
        "primary",
        "secondary",
        "warning",
        "error",
        "success",
        "accent",
        "foreground",
        "background",
        "surface",
        "panel",
        "boost",
    ):
        assert _effective(after, field) == _effective(before, field)
    for capture, style in before_syntax.syntax_styles.items():
        rt_style = after_syntax.syntax_styles[capture]
        assert style.color is not None
        assert rt_style.color is not None
        assert style.color.get_truecolor().hex == rt_style.color.get_truecolor().hex


def test_export_with_overrides_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[tui.theme]\nname = "textual-light"\n\n'
        '[tui.theme.colors]\nprimary = "#ff0000"\n\n'
        '[tui.theme.syntax]\nheading = "#00ff00"\n',
        encoding="utf-8",
    )
    result = runner.invoke(cli_app, ["theme", "export"])
    assert result.exit_code == 0, result.output

    before = resolve_theme(load_settings())
    (tmp_path / "config.toml").write_text(result.output, encoding="utf-8")
    after = resolve_theme(load_settings())
    assert after.name == "textual-light" == before.name
    assert after.primary == before.primary == "#ff0000"


def test_export_name_argument_previews_a_different_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = _run_export(monkeypatch, tmp_path, "textual-light")
    parsed = tomllib.loads(output)
    assert parsed["tui"]["theme"]["name"] == "textual-light"


def test_export_name_argument_keeps_configured_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[tui.theme]\nname = "textual-dark"\n\n'
        '[tui.theme.colors]\nprimary = "#ff0000"\n',
        encoding="utf-8",
    )
    result = runner.invoke(cli_app, ["theme", "export", "textual-light"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(result.output)
    assert parsed["tui"]["theme"]["name"] == "textual-light"
    assert parsed["tui"]["theme"]["colors"]["primary"] == "#ff0000"


def test_export_unknown_theme_name_fails_cleanly_naming_the_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    result = runner.invoke(cli_app, ["theme", "export", "not-a-real-theme"])
    assert result.exit_code == 1
    assert "not-a-real-theme" in result.output

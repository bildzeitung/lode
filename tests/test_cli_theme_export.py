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


# --- [cli.theme.styles] section (lode-mk9j) ----------------------------------


def test_export_includes_the_cli_section_with_every_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lode.cli import CLI_STYLES

    output = _run_export(monkeypatch, tmp_path)
    parsed = tomllib.loads(output)
    styles = parsed["cli"]["theme"]["styles"]
    for name in CLI_STYLES:
        assert name.replace(".", "_") in styles


def test_export_cli_section_default_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lode.cli import resolve_cli_styles

    output = _run_export(monkeypatch, tmp_path)
    before = resolve_cli_styles(load_settings())

    (tmp_path / "config.toml").write_text(output, encoding="utf-8")
    after = resolve_cli_styles(load_settings())
    assert after == before


def test_export_cli_section_with_overrides_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lode.cli import resolve_cli_styles

    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[cli.theme.styles]\nnote_id = "bold magenta"\n', encoding="utf-8"
    )
    result = runner.invoke(cli_app, ["theme", "export"])
    assert result.exit_code == 0, result.output

    before = resolve_cli_styles(load_settings())
    assert before["note_id"] == "bold magenta"

    (tmp_path / "config.toml").write_text(result.output, encoding="utf-8")
    after = resolve_cli_styles(load_settings())
    assert after == before


# --- resolve settings at most once per invocation (lode-9otn) ----------------


def test_export_resolves_settings_at_most_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``lode theme export`` must not stat/read/parse config.toml twice.

    ``main()``'s global [cli.theme] wiring (lode-mk9j) already resolves
    settings once for every invocation; ``theme_export`` used to resolve the
    identical config a second time. Count ``load_settings`` calls directly
    (the load-bearing assertion) rather than inferring it from output.
    """
    import lode.cli as cli_mod

    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    calls = 0
    real_load_settings = cli_mod.load_settings

    def _counting_load_settings(**overrides: object) -> object:
        nonlocal calls
        calls += 1
        return real_load_settings(**overrides)

    # `_resolve_settings()` calls the name `load_settings` bound into
    # `lode.cli`'s own namespace (`from lode.config import ... load_settings`),
    # so that's the reference to intercept -- patching `lode.config
    # .load_settings` instead would miss every call `cli._resolve_settings`
    # makes.
    monkeypatch.setattr(cli_mod, "load_settings", _counting_load_settings)

    result = runner.invoke(cli_app, ["theme", "export"])
    assert result.exit_code == 0, result.output
    assert calls == 1, f"expected exactly one load_settings() call, got {calls}"


def test_export_reports_a_broken_config_file_at_most_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken config.toml must not print the 'invalid config file' line
    twice -- one per (would-be) resolution.

    ``main()`` resolves settings before dispatching to any subcommand but
    ``status`` (lode-mk9j), so today this already aborts before
    ``theme_export`` ever runs; the assertion pins the invariant against a
    regression from either layer resolving twice.
    """
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("not_a_real_knob = 1\n", encoding="utf-8")

    result = runner.invoke(cli_app, ["theme", "export"])
    assert result.stderr.count("invalid config file") <= 1, result.output

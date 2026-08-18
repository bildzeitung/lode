"""Tests for the ``[tui.theme]`` config section and its startup wiring (lode-cwyk).

Design settled at ``lode-5zxt`` (``docs/decisions.md`` "lode-dmbc" entry,
2026-08-17 update): a base Textual theme name plus fixed-key-set
``[tui.theme.colors]``/closed-key-set ``[tui.theme.syntax]`` overrides,
validated at config load (``textual.color.Color.parse``), precedence base ->
colors -> syntax, absent section leaves current defaults byte-identical.

Covers: config validation (good/bad colours, unknown keys, unknown theme
name all rejected, naming the offending key), precedence (an override wins
over the base theme's own value), and startup wiring (``LodeApp`` registers
and activates the effective ``Theme`` only when configured; the note-body
``TextAreaTheme`` used by ``_markdown_text_area`` reflects ``syntax``
overrides). ``lode theme export``'s own round-trip test lives in
``tests/test_cli_theme_export.py``.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError
from textual.theme import BUILTIN_THEMES

from lode.config import Settings, TuiTheme, TuiThemeColors, TuiThemeSyntax
from lode.theming import (
    SYNTAX_KEY_TO_CAPTURE,
    resolve_note_body_theme,
    resolve_theme,
)
from lode.tui.app import LodeApp
from lode.tui.screens._markdown_area import NOTE_BODY_SYNTAX_STYLES, NOTE_BODY_THEME

# --- Absent section: byte-identical defaults ---------------------------------


def test_tui_theme_defaults_to_none() -> None:
    assert Settings().tui.theme is None


def test_absent_section_resolves_to_textual_dark_chrome() -> None:
    theme = resolve_theme(Settings())
    assert theme.name == "textual-dark"
    assert theme is BUILTIN_THEMES["textual-dark"]


def test_absent_section_resolves_to_the_same_note_body_theme_singleton() -> None:
    # Identity, not just equality -- the acceptance criterion is
    # "byte-identical", and this is the strongest form of that.
    assert resolve_note_body_theme(Settings()) is NOTE_BODY_THEME


def test_app_leaves_default_theme_untouched_when_section_absent() -> None:
    app = LodeApp(settings=Settings())
    assert app.theme == "textual-dark"
    assert app.note_body_theme is NOTE_BODY_THEME


# --- Config validation: good/bad colours, unknown keys, unknown theme name ---


def test_valid_theme_config_loads() -> None:
    settings = Settings(
        tui={
            "theme": {
                "name": "textual-light",
                "colors": {"primary": "#ff0000"},
                "syntax": {"heading_marker": "#00ff00"},
            }
        }
    )
    assert settings.tui.theme is not None
    assert settings.tui.theme.name == "textual-light"
    assert settings.tui.theme.colors.primary == "#ff0000"
    assert settings.tui.theme.syntax.heading_marker == "#00ff00"


def test_unknown_theme_name_rejected_naming_the_key() -> None:
    with pytest.raises(ValidationError, match="tui.theme.name.*nonexistent-theme"):
        Settings(tui={"theme": {"name": "nonexistent-theme"}})


def test_invalid_colour_value_rejected_naming_the_key() -> None:
    with pytest.raises(ValidationError, match="tui.theme.colors.primary"):
        Settings(tui={"theme": {"colors": {"primary": "not-a-colour"}}})


def test_invalid_syntax_colour_value_rejected_naming_the_key() -> None:
    with pytest.raises(ValidationError, match="tui.theme.syntax.heading"):
        Settings(tui={"theme": {"syntax": {"heading": "not-a-colour"}}})


def test_unknown_colors_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Settings(tui={"theme": {"colors": {"not_a_real_variable": "#ff0000"}}})


def test_unknown_syntax_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Settings(tui={"theme": {"syntax": {"not_a_real_capture": "#ff0000"}}})


def test_unknown_theme_section_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Settings(tui={"theme": {"unknown_field": 1}})


def test_every_documented_colors_key_accepts_a_colour() -> None:
    # Non-vacuousness for the "fixed key set" claim: every key TuiThemeColors
    # declares actually round-trips through Color.parse without raising.
    overrides = {key: "#123456" for key in TuiThemeColors.model_fields}
    colors = TuiThemeColors(**overrides)
    for key in overrides:
        assert getattr(colors, key) == "#123456"


def test_every_documented_syntax_key_accepts_a_colour() -> None:
    overrides = {key: "#123456" for key in TuiThemeSyntax.model_fields}
    syntax = TuiThemeSyntax(**overrides)
    for key in overrides:
        assert getattr(syntax, key) == "#123456"


def test_syntax_config_keys_and_capture_mapping_cannot_drift() -> None:
    # TuiThemeSyntax declares the config fields; SYNTAX_KEY_TO_CAPTURE is
    # derived from NOTE_BODY_SYNTAX_STYLES. Nothing mechanically ties the two,
    # so a capture added to the palette without a matching config field (or
    # vice versa) would silently be un-overridable and un-exportable. Pin them.
    assert set(TuiThemeSyntax.model_fields) == set(SYNTAX_KEY_TO_CAPTURE)
    assert set(SYNTAX_KEY_TO_CAPTURE.values()) == set(NOTE_BODY_SYNTAX_STYLES)


def test_colors_config_keys_match_the_theme_fields_they_override() -> None:
    # Every [tui.theme.colors] key must name a real textual Theme field, or
    # dataclasses.replace() in resolve_theme_from would raise at startup.
    theme_fields = {f.name for f in dataclasses.fields(BUILTIN_THEMES["textual-dark"])}
    assert set(TuiThemeColors.model_fields) <= theme_fields


# --- Precedence: base -> colors overrides -> syntax overrides ----------------


def test_colors_override_wins_over_base_theme_value() -> None:
    base = BUILTIN_THEMES["textual-dark"]
    assert base.primary != "#ff0000"  # sanity: this IS an override, not a no-op
    settings = Settings(
        tui={"theme": {"name": "textual-dark", "colors": {"primary": "#ff0000"}}}
    )
    theme = resolve_theme(settings)
    assert theme.primary == "#ff0000"
    # Untouched fields keep the base's own value.
    assert theme.secondary == base.secondary


def test_syntax_override_wins_over_note_body_default() -> None:
    default_capture = NOTE_BODY_SYNTAX_STYLES["heading.marker"].color
    assert default_capture is not None
    settings = Settings(tui={"theme": {"syntax": {"heading_marker": "#00ff00"}}})
    text_area_theme = resolve_note_body_theme(settings)
    overridden = text_area_theme.syntax_styles["heading.marker"].color
    assert overridden is not None
    assert overridden.get_truecolor().hex.lower() == "#00ff00"
    # A capture NOT overridden keeps the module default.
    assert (
        text_area_theme.syntax_styles["text.literal"]
        == NOTE_BODY_SYNTAX_STYLES["text.literal"]
    )


def test_absent_colors_section_falls_back_to_base_theme_values() -> None:
    settings = Settings(tui={"theme": {"name": "textual-light"}})
    theme = resolve_theme(settings)
    base = BUILTIN_THEMES["textual-light"]
    assert theme.primary == base.primary


# --- Startup wiring: LodeApp registers + activates the effective theme ------


def test_app_registers_and_activates_configured_theme() -> None:
    settings = Settings(
        tui={"theme": {"name": "textual-light", "colors": {"primary": "#ff0000"}}}
    )
    app = LodeApp(settings=settings)
    assert app.theme == "textual-light"
    registered = app.get_theme("textual-light")
    assert registered is not None
    assert registered.primary == "#ff0000"


def test_app_note_body_theme_reflects_syntax_override() -> None:
    settings = Settings(tui={"theme": {"syntax": {"list_marker": "#abcdef"}}})
    app = LodeApp(settings=settings)
    style = app.note_body_theme.syntax_styles["list.marker"]
    assert style.color is not None
    assert style.color.get_truecolor().hex.lower() == "#abcdef"


# --- config.toml precedence, end to end --------------------------------------


def test_theme_config_loads_from_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[tui.theme]\nname = "textual-light"\n\n'
        '[tui.theme.colors]\nprimary = "#ff0000"\n\n'
        '[tui.theme.syntax]\nheading = "#00ff00"\n',
        encoding="utf-8",
    )
    from lode.config import load_settings

    settings = load_settings()
    assert settings.tui.theme is not None
    assert settings.tui.theme.name == "textual-light"
    assert settings.tui.theme.colors.primary == "#ff0000"
    assert settings.tui.theme.syntax.heading == "#00ff00"


def test_bad_colour_in_config_toml_fails_at_load_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[tui.theme.colors]\nprimary = "not-a-colour"\n', encoding="utf-8"
    )
    from lode.config import load_settings

    with pytest.raises(ValidationError, match="tui.theme.colors.primary"):
        load_settings()


# --- knob_rows renders the nested section sanely, not a raw repr ------------


def test_knob_rows_renders_tui_default_as_default_marker() -> None:
    from lode.config import knob_rows

    rows = {name: value for name, value, _kind in knob_rows(Settings())}
    assert rows["tui"] == "(default)"


def test_knob_rows_renders_configured_tui_theme_with_override_counts() -> None:
    from lode.config import knob_rows

    settings = Settings(
        tui={
            "theme": {
                "name": "textual-light",
                "colors": {"primary": "#ff0000", "accent": "#00ff00"},
                "syntax": {"heading": "#0000ff"},
            }
        }
    )
    rows = {name: value for name, value, _kind in knob_rows(settings)}
    assert "textual-light" in rows["tui"]
    assert "2 colour override" in rows["tui"]
    assert "1 syntax override" in rows["tui"]


# --- TuiTheme itself is a plain, reusable BaseModel ---------------------------


def test_tui_theme_default_name_is_textual_dark() -> None:
    assert TuiTheme().name == "textual-dark"

"""Tests for the ``[cli.theme]`` config section and its startup wiring
(lode-mk9j, follow-on to lode-cwyk's ``[tui.theme]``).

Design settled at ``lode-mk9j`` (``docs/decisions.md``'s ``lode-mk9j`` entry,
2026-08-18): a fixed key set ``[cli.theme.styles]`` overriding
``lode.cli.CLI_STYLES``'s semantic style names, validated at config load with
``rich.style.Style.parse`` (NOT ``textual.color.Color.parse`` -- ``CLI_STYLES``'s
own defaults are rich STYLE strings like ``"bold red"``/``"dim"``/``"bold"``,
not bare colours), absent section leaves current defaults byte-identical.

Covers: config validation (good/bad styles, unknown keys rejected naming the
key), the key-set/semantic-name mapping never drifting, and
``resolve_cli_styles``'s resolution (override wins, absent falls back,
absent-section identity). ``main()``'s global apply-with-status-exemption
wiring and ``lode theme export``'s round trip each have their own test
modules (``tests/test_cli.py``, ``tests/test_cli_theme_export.py``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lode.cli import CLI_STYLE_KEY_TO_NAME, CLI_STYLES, resolve_cli_styles
from lode.config import CliThemeStyles, Settings

# --- Absent section: byte-identical defaults ---------------------------------


def test_cli_theme_defaults_to_none() -> None:
    assert Settings().cli.theme is None


def test_absent_section_resolves_to_cli_styles_identity() -> None:
    # Identity, not just equality -- "absent section leaves defaults
    # byte-identical" is the acceptance criterion, and this is the strongest
    # form of that assertion.
    assert resolve_cli_styles(Settings()) is CLI_STYLES


# --- Config validation: good/bad styles, unknown keys ------------------------


def test_valid_theme_config_loads() -> None:
    settings = Settings(cli={"theme": {"styles": {"note_id": "bold magenta"}}})
    assert settings.cli.theme is not None
    assert settings.cli.theme.styles.note_id == "bold magenta"


def test_invalid_style_value_rejected_naming_the_key() -> None:
    with pytest.raises(ValidationError, match="cli.theme.styles.note_id"):
        Settings(cli={"theme": {"styles": {"note_id": "not a real style xyz"}}})


def test_unknown_styles_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Settings(cli={"theme": {"styles": {"not_a_real_style": "bold"}}})


def test_unknown_theme_section_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Settings(cli={"theme": {"unknown_field": 1}})


def test_every_documented_styles_key_accepts_a_style() -> None:
    # Non-vacuousness for the "fixed key set" claim: every key
    # CliThemeStyles declares actually round-trips through Style.parse
    # without raising -- including the ones whose CLI_STYLES default is a
    # multi-word rich style string, not a bare colour.
    overrides = {key: "bold magenta" for key in CliThemeStyles.model_fields}
    styles = CliThemeStyles(**overrides)
    for key in overrides:
        assert getattr(styles, key) == "bold magenta"


def test_cli_styles_own_defaults_are_valid_style_strings() -> None:
    # The exact concern the maintainer's decision named: rich.style.Style.parse
    # must accept CLI_STYLES's own default values (e.g. "bold red", "dim"),
    # unlike textual.color.Color.parse, which would reject them.
    overrides = dict(CLI_STYLES)
    del overrides["table.header"]  # not a [cli.theme.styles] key -- see below
    settings = Settings(
        cli={
            "theme": {
                "styles": {
                    key.replace(".", "_"): value for key, value in overrides.items()
                }
            }
        }
    )
    assert settings.cli.theme is not None


# --- Key-set / semantic-name mapping cannot drift -----------------------------


def test_styles_config_keys_and_semantic_name_mapping_cannot_drift() -> None:
    # CliThemeStyles declares the config fields; CLI_STYLE_KEY_TO_NAME is
    # derived from CLI_STYLES. Nothing mechanically ties the two together, so
    # a name added to CLI_STYLES without a matching config field (or vice
    # versa) would silently be un-overridable and un-exportable. Pin them.
    assert set(CliThemeStyles.model_fields) == set(CLI_STYLE_KEY_TO_NAME)
    assert set(CLI_STYLE_KEY_TO_NAME.values()) == set(CLI_STYLES)


# --- resolve_cli_styles: override wins, absent falls back --------------------


def test_style_override_wins_over_cli_styles_default() -> None:
    assert CLI_STYLES["note_id"] != "bold magenta"  # sanity: an override, not a no-op
    settings = Settings(cli={"theme": {"styles": {"note_id": "bold magenta"}}})
    resolved = resolve_cli_styles(settings)
    assert resolved["note_id"] == "bold magenta"
    # Untouched names keep CLI_STYLES's own value.
    assert resolved["date"] == CLI_STYLES["date"]


def test_absent_styles_section_falls_back_to_cli_styles_defaults() -> None:
    settings = Settings(cli={"theme": {}})
    resolved = resolve_cli_styles(settings)
    assert resolved == CLI_STYLES


def test_table_header_key_is_table_underscore_header() -> None:
    # The one name in CLI_STYLES with a literal "." -- TOML cannot key a
    # table with a bare "." in a bare key, so it maps to table_header.
    assert CLI_STYLE_KEY_TO_NAME["table_header"] == "table.header"
    settings = Settings(cli={"theme": {"styles": {"table_header": "italic"}}})
    resolved = resolve_cli_styles(settings)
    assert resolved["table.header"] == "italic"

"""``lode theme export`` -- print the fully-resolved effective TUI theme as TOML.

The escape hatch the ``[tui.theme]`` design settled at ``lode-5zxt`` promises
(``docs/decisions.md`` "lode-dmbc" entry, 2026-08-17 update, point 4): rather
than type override keys from memory, a user runs this, pastes the output into
``config.toml``, and edits from there.

Prints every ``[tui.theme.colors]`` variable and every ``[tui.theme.syntax]``
capture, fully resolved -- never a partial override list -- so the emitted
block is self-contained and round-trips: pasting it back in reproduces the
same effective theme (its own values already ARE the effective ones).
"""

from typing import Annotated

import typer
from pydantic import ValidationError

from lode import cli
from lode.cli import app
from lode.config import (
    TUI_THEME_COLOR_KEYS,
    TuiTheme,
    TuiThemeColors,
    TuiThemeSyntax,
)
from lode.theming import (
    SYNTAX_KEY_TO_CAPTURE,
    resolve_note_body_theme_from,
    resolve_theme_from,
)

theme_app = typer.Typer(
    help="Inspect and export the TUI's [tui.theme] configuration.",
    no_args_is_help=True,
)
app.add_typer(theme_app, name="theme")


@theme_app.command(
    "export",
    help=(
        "Print the fully-resolved effective TUI theme as ready-to-paste TOML.\n\n"
        "Every [tui.theme.colors]/[tui.theme.syntax] key is printed, fully "
        "resolved -- not just what you've overridden -- so the block is "
        "self-contained: paste it into config.toml as your new [tui.theme] "
        "and it reproduces exactly what you see today.\n\n"
        "Pass NAME to preview a different base theme instead of the one in "
        "your config; your current colour/syntax overrides still apply on "
        "top of it."
    ),
)
def theme_export(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Base Textual theme to preview instead of the configured one."
        ),
    ] = None,
) -> None:
    """Print the fully-resolved effective TUI theme as ready-to-paste TOML.

    Resolution mirrors startup wiring exactly (:mod:`lode.theming`):
    base theme name -> ``[tui.theme.colors]`` overrides -> ``[tui.theme.syntax]``
    overrides. With no ``[tui.theme]`` configured, the base is Textual's own
    ``textual-dark`` and there are no overrides -- so the printed block is the
    unmodified default, still valid to paste in as a starting point.

    ``NAME`` overrides only the base theme name for this preview; any
    ``colors``/``syntax`` overrides already in ``config.toml`` still apply on
    top of it, exactly as they would after editing ``name`` there yourself.

    A bad ``NAME`` (not a Textual-registered theme) fails the same clean way
    every other bad-input CLI error here does: a one-line stderr message
    naming the value, exit 1 -- never a traceback.
    """
    settings = cli._resolve_settings()
    configured = settings.tui.theme
    base_name = (
        name
        if name is not None
        else (configured.name if configured else "textual-dark")
    )
    colors = configured.colors if configured is not None else TuiThemeColors()
    syntax = configured.syntax if configured is not None else TuiThemeSyntax()
    try:
        theme_cfg = TuiTheme(name=base_name, colors=colors, syntax=syntax)
    except ValidationError as exc:
        typer.echo(f"lode theme export: {exc}", err=True)
        raise typer.Exit(code=1) from None

    theme = resolve_theme_from(theme_cfg)
    note_body_theme = resolve_note_body_theme_from(theme_cfg)
    # to_color_system().generate() is a BLENDED token derivation (it slightly
    # perturbs even an explicitly-set colour, e.g. "#ff0000" -> "#FE0000" --
    # verified empirically), so it can only be trusted for a field the base
    # theme itself leaves unset (background/surface/panel/boost default to
    # None -- Theme/ColorSystem derive a value for them at generate() time).
    # Every field WITH a literal value (base or overridden) is read straight
    # off the Theme dataclass instead -- the exact string that was set, byte
    # for byte -- which is what makes the round trip exact.
    color_system = theme.to_color_system().generate()

    lines = ["[tui.theme]", f'name = "{theme_cfg.name}"', "", "[tui.theme.colors]"]
    for key in TUI_THEME_COLOR_KEYS:
        literal = getattr(theme, key)
        lines.append(
            f'{key} = "{literal if literal is not None else color_system[key]}"'
        )
    lines.append("")
    lines.append("[tui.theme.syntax]")
    for key, capture in SYNTAX_KEY_TO_CAPTURE.items():
        style = note_body_theme.syntax_styles[capture]
        assert style.color is not None  # every default/override sets one
        lines.append(f'{key} = "{style.color.get_truecolor().hex}"')
    typer.echo("\n".join(lines))

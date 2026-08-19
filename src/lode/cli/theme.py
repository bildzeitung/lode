"""``lode theme export`` -- print the fully-resolved effective TUI + CLI theme
as TOML.

The escape hatch the ``[tui.theme]`` design settled at ``lode-5zxt`` promises
(``docs/decisions.md`` "lode-dmbc" entry, 2026-08-17 update, point 4): rather
than type override keys from memory, a user runs this, pastes the output into
``config.toml``, and edits from there. ``lode-mk9j`` extends the same escape
hatch to ``[cli.theme.styles]``.

Prints every ``[tui.theme.colors]`` variable, every ``[tui.theme.syntax]``
capture, and every ``[cli.theme.styles]`` name, fully resolved -- never a
partial override list -- so the emitted block is self-contained and
round-trips: pasting it back in reproduces the same effective theme (its own
values already ARE the effective ones).
"""

import logging
from typing import Annotated

import typer
from pydantic import ValidationError

from lode import cli
from lode.cli import app
from lode.config import TUI_THEME_COLOR_KEYS, Settings, TuiTheme

log = logging.getLogger(__name__)

theme_app = typer.Typer(
    help="Inspect and export the TUI's [tui.theme] and CLI's [cli.theme] configuration.",
    no_args_is_help=True,
)
app.add_typer(theme_app, name="theme")


@theme_app.command(
    "export",
    help=(
        "Print the fully-resolved effective TUI + CLI theme as ready-to-paste TOML.\n\n"
        "Every [tui.theme.colors]/[tui.theme.syntax]/[cli.theme.styles] key is "
        "printed, fully resolved -- not just what you've overridden -- so the "
        "block is self-contained: paste it into config.toml as your new "
        "[tui.theme]/[cli.theme] and it reproduces exactly what you see today.\n\n"
        "Pass NAME to preview a different base TUI theme instead of the one in "
        "your config; your current overrides (TUI and CLI alike) still apply "
        "on top of it.\n\n"
        "If config.toml is broken (bad syntax, unreadable, or an invalid theme "
        "value), this still succeeds: it warns on stderr and prints the "
        "built-in defaults instead, so you can paste over the broken block."
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
    """Print the fully-resolved effective TUI + CLI theme as ready-to-paste TOML.

    Resolution mirrors startup wiring exactly (:mod:`lode.theming` for the
    TUI section; :func:`lode.cli.resolve_cli_styles` for the CLI one):
    base theme name -> ``[tui.theme.colors]`` overrides -> ``[tui.theme.syntax]``
    overrides, then ``lode.cli.CLI_STYLES`` -> ``[cli.theme.styles]``
    overrides. With no ``[tui.theme]``/``[cli.theme]`` configured, the base is
    Textual's own ``textual-dark`` / ``CLI_STYLES`` unmodified -- so the
    printed block is the unmodified default, still valid to paste in as a
    starting point.

    ``NAME`` overrides only the TUI base theme name for this preview; every
    other override already in ``config.toml`` (TUI colors/syntax, CLI
    styles) still applies on top of it, exactly as it would after editing
    ``name`` there yourself.

    A bad ``NAME`` (not a Textual-registered theme) fails the same clean way
    every other bad-input CLI error here does: a one-line stderr message
    naming the value, exit 1 -- never a traceback.

    This command is BEST-EFFORT against a broken ``config.toml`` (lode-jjol):
    it is precisely the tool a user reaches for to recover a known-good
    ``[tui.theme]``/``[cli.theme]`` block, so a config.toml that is
    malformed, unreadable, or carries an invalid theme value never blocks
    it. On a failed settings resolution it prints a one-line warning to
    stderr and falls back to the built-in defaults (an absent config.toml)
    instead of exiting 1.
    """
    # Lazy, function-level import -- the lazy-import convention `lode.cli` and
    # `lode.cli.tui` already follow "to keep CLI startup light". `lode.theming`
    # pulls in textual.theme + textual.widgets.text_area (and, through them,
    # tree_sitter), which measured at ~325ms of the ~660ms `import lode.cli`
    # takes -- a cost every OTHER `lode` subcommand would pay at module level,
    # since `_COMMAND_MODULES` imports this module on every invocation.
    from lode.theming import (
        SYNTAX_KEY_TO_CAPTURE,
        resolve_note_body_theme_from,
        resolve_theme_from,
    )

    # Best-effort settings resolution (lode-jjol): `_resolve_settings()`
    # itself exits 1 on a bad config.toml (malformed TOML, an out-of-range
    # theme value, ...), and a raw, unreadable file raises `OSError`
    # straight through it -- both of which would otherwise stop the ONE
    # command that exists to help a user recover from exactly that config.
    # Fall back to `Settings()` (the same shape `load_settings()` returns
    # for an absent config.toml) so the rest of this command sees ordinary,
    # unconfigured defaults and needs no further special-casing below.
    #
    # Not ``cli._resolve_settings_best_effort()``: this command needs a real
    # ``Settings`` to keep exporting, plus a user-facing stderr line, where
    # that helper's contract is a silent ``None``. The ``except Exception``
    # here is the same broad catch for the same reason, and that helper's
    # docstring owns the why.
    try:
        settings = cli._resolve_settings()
    except Exception:
        # Logged with `exc_info` for the same reason the shared helper does
        # it: the stderr line below is the user's story, the traceback is the
        # maintainer's, and it keeps the broad catch honest (and BLE001
        # satisfied) rather than silently discarded.
        log.debug("theme export: _resolve_settings failed", exc_info=True)
        typer.echo(
            "lode theme export: could not resolve config.toml -- "
            "falling back to built-in defaults",
            err=True,
        )
        settings = Settings()
    # `or TuiTheme()` rather than a hand-written absent-section fallback: the
    # model already carries the base-theme default and both sub-model
    # default_factories, so the default base theme name lives in exactly one
    # place (TuiTheme.name) instead of being retyped here.
    configured = settings.tui.theme or TuiTheme()
    try:
        theme_cfg = TuiTheme(
            name=name if name is not None else configured.name,
            colors=configured.colors,
            syntax=configured.syntax,
        )
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
        # Every entry in NOTE_BODY_SYNTAX_STYLES sets a colour, and an override
        # can only replace it with another, so `.color` is never None here --
        # left unasserted rather than guarded by a bare `assert`, which would
        # vanish under `-O` anyway (the next line raises on its own if it ever
        # became None).
        colour = note_body_theme.syntax_styles[capture].color
        lines.append(f'{key} = "{colour.get_truecolor().hex}"')

    lines.append("")
    lines.append("[cli.theme.styles]")
    resolved_styles = cli.resolve_cli_styles(settings)
    for key, style_name in cli.CLI_STYLE_KEY_TO_NAME.items():
        lines.append(f'{key} = "{resolved_styles[style_name]}"')
    typer.echo("\n".join(lines))

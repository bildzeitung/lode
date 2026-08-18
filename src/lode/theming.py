"""Resolve the effective TUI theme from ``Settings.tui.theme`` (lode-cwyk).

Design settled at ``lode-5zxt`` (see ``docs/decisions.md``'s ``lode-dmbc``
entry, 2026-08-17 update): a base Textual theme name, plus a fixed key set of
``[tui.theme.colors]`` overrides on that theme's variables, plus a closed key
set of ``[tui.theme.syntax]`` overrides on the note-body markdown palette.
Precedence: base name -> ``colors`` overrides -> ``syntax`` overrides.

Two resolvers, one per surface, sharing nothing but the config shape:

- :func:`resolve_theme` -- a ``textual.theme.Theme`` for the app chrome
  (``lode.tcss`` variables), built from ``TuiTheme.colors``.
- :func:`resolve_note_body_theme` -- a ``TextAreaTheme`` for the four
  note-body screens (``lode.tui.screens._markdown_area``), built by merging
  ``TuiTheme.syntax`` over ``NOTE_BODY_SYNTAX_STYLES``.

Both take a ``Settings`` and return the library default (Textual's own
``textual-dark`` / the existing ``NOTE_BODY_THEME`` singleton) when
``settings.tui.theme`` is absent -- the "absent section leaves current
defaults byte-identical" acceptance criterion. Callers that already know
they have a non-``None`` :class:`~lode.config.TuiTheme` (the ``lode theme
export`` CLI command, previewing a theme the current config may not even
have configured) can build one directly with :func:`resolve_theme_from` /
:func:`resolve_note_body_theme_from` instead of threading a whole
``Settings`` through.
"""

from __future__ import annotations

import dataclasses

from rich.style import Style
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets.text_area import TextAreaTheme

from lode.config import Settings, TuiTheme
from lode.tui.screens._markdown_area import NOTE_BODY_SYNTAX_STYLES, NOTE_BODY_THEME

#: ``[tui.theme.syntax]`` key -> the tree-sitter capture name it overrides in
#: ``NOTE_BODY_SYNTAX_STYLES`` (the ``_``-for-``.`` mapping ``lode-dmbc``
#: settled, so tree-sitter's own vocabulary never becomes config surface).
SYNTAX_KEY_TO_CAPTURE: dict[str, str] = {
    "text_literal": "text.literal",
    "punctuation_delimiter": "punctuation.delimiter",
    "heading_marker": "heading.marker",
    "heading": "heading",
    "list_marker": "list.marker",
}


def resolve_theme_from(theme_cfg: TuiTheme) -> Theme:
    """Build the effective ``Theme`` for an explicit :class:`TuiTheme` config.

    ``theme_cfg.name`` is already validated (at ``Settings`` construction)
    against ``BUILTIN_THEMES``, so the lookup here cannot raise ``KeyError``.
    The returned ``Theme`` keeps the base's own ``name`` -- registering it
    overrides the builtin entry in place (``App.register_theme``'s
    documented behaviour), rather than minting a second, differently-named
    theme.
    """
    base = BUILTIN_THEMES[theme_cfg.name]
    overrides = {
        key: value
        for key, value in theme_cfg.colors.model_dump().items()
        if value is not None
    }
    return dataclasses.replace(base, **overrides) if overrides else base


def resolve_theme(settings: Settings) -> Theme:
    """Build the effective app-chrome ``Theme`` for ``settings.tui.theme``.

    Falls back to Textual's own ``textual-dark`` default when the
    ``[tui.theme]`` section is absent.
    """
    theme_cfg = settings.tui.theme
    if theme_cfg is None:
        return BUILTIN_THEMES["textual-dark"]
    return resolve_theme_from(theme_cfg)


def resolve_note_body_theme_from(theme_cfg: TuiTheme) -> TextAreaTheme:
    """Build the effective note-body ``TextAreaTheme`` for an explicit
    :class:`TuiTheme` config -- ``NOTE_BODY_SYNTAX_STYLES`` with
    ``theme_cfg.syntax`` overrides merged over it.
    """
    styles = dict(NOTE_BODY_SYNTAX_STYLES)
    for key, capture in SYNTAX_KEY_TO_CAPTURE.items():
        value = getattr(theme_cfg.syntax, key)
        if value is not None:
            styles[capture] = Style(color=value)
    return TextAreaTheme(name=NOTE_BODY_THEME.name, syntax_styles=styles)


def resolve_note_body_theme(settings: Settings) -> TextAreaTheme:
    """Build the effective note-body ``TextAreaTheme`` for
    ``settings.tui.theme``.

    Returns the existing :data:`~lode.tui.screens._markdown_area.NOTE_BODY_THEME`
    singleton -- not a freshly-built equivalent -- when ``[tui.theme]`` is
    absent, so the "absent section leaves current defaults byte-identical"
    acceptance criterion holds by identity, not just by value.
    """
    theme_cfg = settings.tui.theme
    if theme_cfg is None:
        return NOTE_BODY_THEME
    return resolve_note_body_theme_from(theme_cfg)

"""Tests for the CLI's shared rich ``Theme`` (lode-l38d.11) -- the semantic
style names the colour/table sibling tickets (lode-l38d.4/.5/.6/.10)
reference by NAME instead of each inventing its own colour literal.

Unlike ``NO_COLOR``/TTY detection (see tests/test_cli_console.py), a
``Theme``'s style mapping is plain data, not something decided by
environment detection frozen at ``Console()`` construction -- so asserting
it in-process (no subprocess needed) exercises the real thing.
"""

from __future__ import annotations

from rich.style import Style

import lode.cli

#: The style names this ticket's acceptance criteria require: one per
#: consumer ticket (lode-l38d.5's note_id/date, matched by lode-l38d.10;
#: lode-l38d.6's warn/danger/ok; lode-l38d.4's table.header).
_EXPECTED_STYLES = {
    "note_id": "cyan",
    "date": "dim",
    "warn": "yellow",
    "danger": "bold red",
    "ok": "bold green",
    "table.header": "bold",
}


def test_cli_theme_defines_the_semantic_styles_the_siblings_need() -> None:
    """Every style name the four colour/table consumer tickets need exists
    on ``CLI_THEME``, mapped to the style this ticket decided."""
    for name, spec in _EXPECTED_STYLES.items():
        assert lode.cli.CLI_THEME.styles[name] == Style.parse(spec)


def test_shared_console_resolves_each_style_through_its_theme() -> None:
    """The shared ``console`` -- what every command actually renders
    through -- resolves each semantic name to the same style, proving the
    theme is attached to it rather than merely existing standalone."""
    for name, spec in _EXPECTED_STYLES.items():
        assert lode.cli.console.get_style(name) == Style.parse(spec)

"""Tests for the CLI's shared rich ``Theme`` (lode-l38d.11) -- the semantic
style names the colour/table sibling tickets (lode-l38d.4/.5/.6/.10)
reference by NAME instead of each inventing its own colour literal.

Unlike ``NO_COLOR``/TTY detection (see tests/test_cli_console.py), a
``Theme``'s style mapping is plain data, not something decided by
environment detection frozen at ``Console()`` construction -- so asserting
it in-process (no subprocess needed) exercises the real thing.

NON-VACUOUSNESS, demonstrated by sabotaging the SUBJECT (``src/lode/cli/__init__.py``)
rather than by argument -- the bar tests/test_cli_console.py set after
lode-xgaa. Re-verified against rich 15.0.0 during this ticket's technical
review:

* ``"note_id": "magenta"`` (palette silently changed) -> both tests FAIL.
* ``console = Console()`` (theme detached from the shared Console) ->
  ``test_shared_console_resolves_each_style_through_its_theme`` FAILS: five
  of the six names are not rich styles at all, so ``get_style`` raises
  ``MissingStyle``. That is what proves the theme is ATTACHED to the console
  rather than merely existing standalone.
* deleting a line from ``CLI_STYLES`` -> the declaration test FAILS.

THE ONE THING THESE TESTS CANNOT PROVE, recorded so it is not mistaken for
coverage: ``table.header`` is a deliberate restatement of rich's own default
(``DEFAULT_STYLES["table.header"]`` is already ``bold``) and ``Theme``
inherits rich's defaults, so ``CLI_THEME.styles["table.header"]`` and
``console.get_style("table.header")`` both resolve to ``Style(bold=True)``
whether or not ``cli/__init__.py`` declares it. No assertion against the constructed
``Theme``/``Console`` can tell the two apart -- ``Theme.__init__`` copies
``DEFAULT_STYLES`` and ``.update()``s over it, destroying the declaration.
That is why the first test asserts ``CLI_STYLES``, the declaration the
subject deliberately keeps reachable, and not the merged mapping.

The ORIGINAL form of these tests asserted ``CLI_THEME.styles[name]`` and so
passed with the ``table.header`` entry deleted -- a green test for a style
name lode-l38d.4 depends on, proving only that rich has a default. Do not
regress to it.
"""

from __future__ import annotations

from rich.style import Style

import lode.cli

#: The style names this ticket's acceptance criteria require: one per
#: consumer ticket (lode-l38d.5's note_id/date, matched by lode-l38d.10;
#: lode-l38d.6's warn/danger/ok; lode-l38d.4's table.header). Hard-coded
#: here, independently of the subject, so the assertions below are
#: change-detectors rather than tautologies.
_EXPECTED_STYLES = {
    "note_id": "cyan",
    "date": "dim",
    "warn": "yellow",
    "danger": "bold red",
    "ok": "bold green",
    "table.header": "bold",
}


def test_cli_styles_declares_exactly_the_semantic_styles_the_siblings_need() -> None:
    """``CLI_STYLES`` declares exactly the names the four colour/table
    consumer tickets need, mapped to the styles this ticket decided -- no
    fewer, and no more (the ticket's scope is "and no more").

    Asserted against the DECLARATION, not ``CLI_THEME.styles``: the latter is
    merged over rich's ~150 defaults and cannot detect a dropped
    ``table.header`` (see this module's docstring).
    """
    assert lode.cli.CLI_STYLES == _EXPECTED_STYLES


def test_shared_console_resolves_each_style_through_its_theme() -> None:
    """The shared ``console`` -- what every command actually renders through
    -- resolves each semantic name to the decided style, proving the theme is
    attached to it rather than merely existing standalone.

    Load-bearing for five of the six names; ``table.header`` would resolve
    identically through rich's defaults with no theme attached, so it rides
    along here and is pinned by the declaration test above instead.
    """
    for name, spec in _EXPECTED_STYLES.items():
        assert lode.cli.console.get_style(name) == Style.parse(spec)

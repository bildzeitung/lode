"""Gate: every registered CLI command's ``--help`` output obeys the ``help=``
rules pinned in ``docs/conventions.md`` (lode-ii25.9).

## What this enforces

``docs/conventions.md``'s "Typer: user-facing help via ``help=``, rationale in
the docstring" section pins six rules for the text ``--help`` renders, at a
FIXED width (``COLUMNS=80``, matching that section's own "rendered at
COLUMNS=80" language):

1. No Sphinx roles (``:func:``/``:meth:``/``:class:``/``:mod:``/``:data:``/``:attr:``).
2. No RST double-backticks.
3. No bd issue ids (the ``lode-`` pattern).
4. No test-function names (a ``test_`` identifier).
5. A command's own help is at most 12 rendered lines; each option's or
   argument's help is at most 3 rendered lines.
6. The first line of a command's help stands alone as a single plain
   sentence -- carrying none of rules 1-4's violations -- since that is what
   Typer derives ``short_help`` from for the ``lode --help`` command list.

This gate walks EVERY registered command reachable from the top-level
``app`` (``lode.cli.app``), including sub-app commands (``lode models
pull``), invokes ``--help`` through Typer's own ``CliRunner``, and checks
the rendered output against all six rules.

## Why this doesn't fix any command's help text

lode-ii25.2 through lode-ii25.7 (sibling tickets) are the ones that add an
explicit, short ``help=`` to each command going forward. Until a given
command gets one, Typer renders its full maintainer docstring as the
``--help`` text -- which is exactly what ``docs/conventions.md`` documents
these six rules were written FOR, not a symptom this gate should paper over.
So :data:`ALLOWLIST` below is pre-seeded with every command that violates
any rule TODAY, each with a one-line reason (no config file, no marker
comment in CLI source -- this reviewed diff is the entire vetting
mechanism, matching the escape-hatch shape ``docs/conventions.md`` itself
prescribes). A command is added here for VIOLATING rules the day it was
first written, not later.

SABOTAGE-VERIFIED (matching the precedent in ``tests/test_bd_list_limit_gate.py``
and ``tests/test_validate_sha40_call_sites.py``): removing an entry from
:data:`ALLOWLIST` while its command's help still violates a rule fails this
test -- re-confirmed at technical review by deleting the ``add`` and
``status`` entries in-memory, which surfaced ``rule 5 (command help 16 lines
> 12)`` and four ``status`` violations respectively. The assertions below are
not vacuous.

## Rendered-output parsing, not source parsing

Every check runs against ``CliRunner``'s captured ``--help`` OUTPUT, at
``COLUMNS=80`` -- not against the raw docstring/``help=`` string -- because
that output is what a user actually sees, and it is Typer's own rich
renderer (box-drawn Arguments/Options panels) that performs the wrapping
this gate measures. Parsing that renderer's box-drawing output is the one
piece of this file worth documenting carefully:

- **Command help body**: every line between the ``Usage: ...`` line and the
  first panel's top border (``╭─ ...``), with the panel's own leading/
  trailing blank spacer lines trimmed. Rule 5's 12-line cap applies to this.
- **Panel entries** (inside an ``Arguments``/``Options`` panel): each
  option or argument renders as one or more physical lines inside the box.
  An ENTRY-START line is distinguished from a WRAPPED CONTINUATION line by
  how far its content is indented past the box's mandatory single leading
  space: an entry-start's name/flag/marker sits within the first few
  columns (``--db``, ``*    question``, ``[ISSUE_OR_PAGE]``, ...); a
  continuation line is indented all the way to the description column
  instead (empirically 20+ columns past the mandatory pad in every panel
  observed in this corpus). :data:`_ENTRY_INDENT_THRESHOLD` draws that line
  with headroom on both sides -- verified against every panel this gate
  currently walks (``tests/test_cli.py``'s own ``CliRunner`` precedent
  confirms box rendering is stable across this repo's pinned rich/typer
  versions).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

import click
import typer.main
from typer.testing import CliRunner

from lode.cli import app

#: Sphinx cross-reference roles -- never belong in user-facing ``--help`` text
#: (they render as literal ``:func:`...``` noise outside Sphinx).
_SPHINX_ROLE_RE = re.compile(r":(?:func|meth|class|mod|data|attr):")

#: RST double-backtick markup -- ditto; renders as literal double backticks
#: to a terminal user, not as emphasis.
_RST_BACKTICK_RE = re.compile(r"``")

#: A bd issue id (e.g. ``lode-ii25.9``, ``lode-crh8``) -- an implementation
#: detail, never something a user reads in ``--help``.
_BD_ID_RE = re.compile(r"\blode-[a-z0-9]+(?:\.[0-9]+)?\b")

#: A test-function name (e.g. ``test_reindex_lexical_indexes_a_purged...``)
#: -- ditto, a maintainer-only reference that leaked into user-facing text.
_TEST_NAME_RE = re.compile(r"\btest_[A-Za-z0-9_]+\b")

#: Rule 5's two caps, both counted in RENDERED lines at COLUMNS=80.
MAX_COMMAND_HELP_LINES = 12
MAX_OPTION_HELP_LINES = 3

#: How far (in columns, past the box's one mandatory leading pad space) an
#: entry-start line's own content may be indented before it is instead
#: read as a wrapped CONTINUATION of the previous entry. See the module
#: docstring's "Panel entries" section for the empirical basis -- every
#: entry-start observed sits at <=3 columns (arguments: ``*``/marker
#: column), every continuation observed sits at >=20.
_ENTRY_INDENT_THRESHOLD = 10

#: The pinned render width. Passed as ``env=`` on every ``CliRunner.invoke``
#: (see :func:`_render_help`), which is hermetic on its own -- verified by
#: rendering under an ambient ``COLUMNS=200`` (still 80-column output), and
#: that the env value genuinely DRIVES the width rather than coinciding with
#: rich's 80-column non-tty default (rendering at 40/120/200 produced 40/120/
#: 200-column output). Deliberately NOT also written to ``os.environ``: that
#: would leak an unrestored global into the rest of the pytest session (and
#: across xdist workers) for no added guarantee.
#:
#: This does NOT contradict ``tests/conftest.py``'s ``set_console_width``
#: fixture, whose docstring records that ``env={"COLUMNS": ...}`` has no
#: effect. That note is about ``lode.cli``'s own long-lived ``console``
#: singleton, which bakes its width at import time; the help text measured
#: here is rendered by Typer's OWN rich console, constructed fresh per
#: invocation, so it reads the environment every time.
COLUMNS = "80"

runner = CliRunner()


#: Every command that violates one or more of the six rules above TODAY,
#: because it has no explicit ``help=`` yet and Typer is rendering its full
#: maintainer docstring instead (see the module docstring's "Why this
#: doesn't fix any command's help text"). Keyed by the command's full,
#: space-joined name (``"models pull"`` for a sub-app command). Adding
#: an explicit, rule-conformant ``help=`` to a command is what removes it
#: from this dict -- that is lode-ii25.2 through .7's job, not this
#: ticket's.
ALLOWLIST: dict[str, str] = {
    "models pull": (
        "No help= yet (lode-ii25.x); the full docstring renders as --help, "
        "cites lode-r4r2/lode-j5r2, and exceeds 12 lines."
    ),
}


@dataclass(frozen=True)
class _Command:
    name: str  # full, space-joined command name, e.g. "models pull"
    args: tuple[str, ...]  # CLI args to reach it, e.g. ("models", "pull")


def _iter_leaf_commands() -> Iterator[_Command]:
    """Walk ``app`` (and every sub-app) yielding one :class:`_Command` per
    registered LEAF command -- never a group itself.

    Duck-typed on ``hasattr(cmd, "list_commands")`` rather than
    ``isinstance(cmd, click.Group)``: verified empirically that Typer's
    ``TyperGroup`` does NOT satisfy ``isinstance(..., click.Group)`` against
    this repo's pinned typer/click (typer vendors its own click fork), so an
    isinstance check silently walks nothing. ``list_commands``/``get_command``
    are the actual API surface this walk needs, and both are present on any
    click-compatible group regardless of which click module it subclasses.
    """
    top = typer.main.get_command(app)
    top_ctx = click.Context(top, info_name="lode")

    def _walk(
        cmd: click.Command, ctx: click.Context, prefix: tuple[str, ...]
    ) -> Iterator[_Command]:
        if hasattr(cmd, "list_commands"):
            for name in cmd.list_commands(ctx):
                sub = cmd.get_command(ctx, name)
                assert sub is not None, (
                    f"registered but unresolvable: {prefix + (name,)}"
                )
                sub_ctx = click.Context(sub, parent=ctx, info_name=name)
                yield from _walk(sub, sub_ctx, prefix + (name,))
        else:
            yield _Command(name=" ".join(prefix), args=prefix)

    yield from _walk(top, top_ctx, ())


#: Strips ANSI SGR escapes (e.g. ``\x1b[1m``) from rendered ``--help`` output.
#: Rich emits these whenever its terminal-color detection says the output is
#: color-capable -- notably it treats the ``GITHUB_ACTIONS`` env var as such
#: even though ``CliRunner`` capture is not a TTY, so CI rendered colored
#: output this gate never saw locally and every plain-text parser below
#: (``Usage:``, the ``╭`` panel border, the indent heuristic) broke.
#:
#: This gate defends at BOTH layers deliberately, and they are not redundant:
#: :func:`_render_help`'s ``env=`` asks Rich not to colorize in the first
#: place (which also covers any non-SGR sequence a regex would miss), and
#: this strip catches whatever Rich colorizes anyway if a future detection
#: heuristic ignores that env -- exactly the surprise that caused this bug.
#: Measured under ``GITHUB_ACTIONS=true``: ``COLUMNS`` alone leaves 5 SGR
#: escapes, adding ``NO_COLOR=1`` leaves 3 (it drops color but not bold), and
#: ``TERM=dumb`` leaves 0; no non-SGR sequence appears at any setting. The
#: escapes are zero-width once stripped, so the COLUMNS=80 line-length rules
#: below are unaffected either way.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _render_help(command: _Command) -> str:
    result = runner.invoke(
        app,
        [*command.args, "--help"],
        env={"COLUMNS": COLUMNS, "NO_COLOR": "1", "TERM": "dumb"},
    )
    assert result.exit_code == 0, (
        f"lode {command.name} --help exited {result.exit_code}:\n{result.output}"
    )
    return _ANSI_ESCAPE_RE.sub("", result.output)


def _command_help_body(output: str) -> list[str]:
    """The rendered command-help lines: between ``Usage: ...`` and the first
    panel's top border, blank spacer lines at each end trimmed (interior
    blank lines -- paragraph breaks -- are kept; they are still rendered
    lines occupying vertical space)."""
    lines = output.splitlines()
    usage_idx = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("Usage:")), None
    )
    assert usage_idx is not None, f"no 'Usage:' line in rendered help:\n{output}"
    panel_starts = [i for i, line in enumerate(lines) if line.strip().startswith("╭")]
    end_idx = panel_starts[0] if panel_starts else len(lines)
    body = lines[usage_idx + 1 : end_idx]
    while body and body[0].strip() == "":
        body.pop(0)
    while body and body[-1].strip() == "":
        body.pop()
    return body


def _panel_entries(output: str) -> list[list[str]]:
    """Every Arguments/Options panel entry in ``output``, each as its own
    list of rendered lines (length 1 for an unwrapped entry). See the
    module docstring's "Panel entries" section for the indent heuristic.
    """
    lines = output.splitlines()
    entries: list[list[str]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("╭"):
            i += 1
            current: list[str] | None = None
            while i < len(lines) and not lines[i].strip().startswith("╰"):
                line = lines[i]
                inner = line[1:-1] if line.startswith("│") else line
                content = inner.removeprefix(" ")
                indent = len(content) - len(content.lstrip(" "))
                if indent <= _ENTRY_INDENT_THRESHOLD or current is None:
                    if current is not None:
                        entries.append(current)
                    current = [line]
                else:
                    current.append(line)
                i += 1
            if current is not None:
                entries.append(current)
        i += 1
    return entries


def _rule_1_to_4_violations(text: str) -> list[str]:
    violations = []
    if _SPHINX_ROLE_RE.search(text):
        violations.append("rule 1 (Sphinx role)")
    if _RST_BACKTICK_RE.search(text):
        violations.append("rule 2 (RST double-backtick)")
    if _BD_ID_RE.search(text):
        violations.append("rule 3 (bd issue id)")
    if _TEST_NAME_RE.search(text):
        violations.append("rule 4 (test-function name)")
    return violations


def _all_violations(output: str) -> list[str]:
    """Every rule violation found in a command's rendered ``--help``."""
    violations = _rule_1_to_4_violations(output)

    body = _command_help_body(output)
    if len(body) > MAX_COMMAND_HELP_LINES:
        violations.append(
            f"rule 5 (command help {len(body)} lines > {MAX_COMMAND_HELP_LINES})"
        )

    for entry in _panel_entries(output):
        if len(entry) > MAX_OPTION_HELP_LINES:
            violations.append(
                f"rule 5 (option/argument help {len(entry)} lines > "
                f"{MAX_OPTION_HELP_LINES}: {entry[0].strip()!r})"
            )

    if body:
        # The first PARAGRAPH -- the run of non-blank body lines starting at
        # body[0], up to the first blank line (or the end of body).
        first_paragraph: list[str] = []
        for line in body:
            if line.strip() == "":
                break
            first_paragraph.append(line)
        if len(first_paragraph) > 1:
            violations.append(
                "rule 6 (first line does not stand alone -- wraps across "
                f"{len(first_paragraph)} rendered lines)"
            )
        first_line_violations = _rule_1_to_4_violations(" ".join(first_paragraph))
        if first_line_violations:
            violations.append(
                "rule 6 (first line carries a rule 1-4 violation: "
                f"{', '.join(first_line_violations)})"
            )

    return violations


@cache
def _violations_by_command() -> dict[str, list[str]]:
    """``{command name: its rule violations}`` for every registered command.

    Cached because both gates below need the same table, and rendering
    ``--help`` for the whole corpus twice is pure waste.
    """
    return {c.name: _all_violations(_render_help(c)) for c in _iter_leaf_commands()}


def test_every_registered_command_help_obeys_the_help_rules_or_is_allowlisted() -> None:
    by_command = _violations_by_command()
    assert by_command, "no commands discovered -- the walk itself is broken"

    unexplained: list[str] = []
    for name, violations in by_command.items():
        if violations and name not in ALLOWLIST:
            unexplained.append(
                f"{name!r}: {'; '.join(violations)} "
                "(not in ALLOWLIST -- add an entry with a reason, or fix the "
                "help text)"
            )

    assert not unexplained, "\n".join(unexplained)


def test_every_registered_command_is_reachable_including_sub_apps() -> None:
    """Non-vacuity for the walk itself: pins that at least one top-level
    command and one sub-app command (``models pull``) are both discovered,
    so a future refactor that silently stops walking into sub-apps (or
    stops registering top-level commands) is caught here rather than by
    the corpus gate quietly shrinking its own coverage."""
    names = {c.name for c in _iter_leaf_commands()}
    assert "add" in names
    assert "models pull" in names


def test_allowlist_entries_still_have_a_command_that_violates_something() -> None:
    """The allowlist is a set of EXCUSES, not a set of commands. A stale
    entry for a command that no longer violates any rule (its help= was
    fixed by a sibling ticket) should be removed -- otherwise a future
    regression on that same command could slip back in unnoticed, since the
    entry would silently keep excusing it. Split out as its own test purely so
    the failure names the hygiene problem ("remove this stale entry") rather
    than being conflated with the gate above ("this command's help violates a
    rule") -- both are ordinary asserts and both fail the suite.
    """
    by_command = _violations_by_command()
    stale = []
    for name in ALLOWLIST:
        if name not in by_command:
            stale.append(f"{name!r}: no such command registered any more")
        elif not by_command[name]:
            stale.append(f"{name!r}: no longer violates any rule -- remove this entry")
    assert not stale, "\n".join(stale)

"""Gate: the top-level ``lode --help`` Commands panel itself is clean.

lode-ii25.7's acceptance criteria are about the literal rendered ``lode
--help`` output -- "No entry in 'lode --help' contains a bd id, RST
backticks, or an internal table/symbol name; each summary is a
plain-language single line." ``tests/test_cli_help_corpus_gate.py``
(lode-ii25.9) already enforces the regex-detectable half of that
continuously, but indirectly: it checks each command's OWN ``help=``/
docstring first line, which Typer uses verbatim as this list's
``short_help`` -- it never renders bare ``lode --help`` and inspects the
Commands panel directly, which is what the acceptance criteria literally
names as its subject. This is a small, additive check of that literal
surface, split into its own file rather than folded into the corpus gate
file -- that file's ``ALLOWLIST`` is a known concurrent merge-conflict
hotspot across the lode-ii25 epic's sibling tickets (lode-pdtd is editing
it in this same fan-out), so a new command's summary changing does not
also require touching this file.

The panel also carries rows the corpus gate structurally cannot reach:
its walk yields LEAF commands only, recursing THROUGH a sub-app group
without ever rendering the group's own ``--help``, so a group's summary
(``models``' "Manage the local fastembed model-weights cache.") is
checked by nothing else today. That gap is the substantive coverage this
file adds; the leaf rows it also scans are belt-and-braces over the
corpus gate's rule 6. Widening that walk to yield groups too -- which
would subject group summaries to all six rules and the allowlist escape
hatch, rather than the two rules below -- is the deeper fix, deferred
only because the corpus-gate file is under concurrent edit in this
fan-out; tracked as its own ticket.

Deliberately does NOT re-derive the corpus gate's own rule catalogue (no
Sphinx-role/test-name/line-length checks here) -- this file checks only
what lode-ii25.7's acceptance criteria names: bd ids and RST backticks in
the top-level list. Internal-symbol-name and plain-language-quality are a
human judgment call, not something a regex can assert; that read is
recorded in this ticket's ``--design`` field, not encoded here.

The ``╭``/``╰`` box-rendering assumptions the panel scan below relies on
are documented once, in ``tests/test_cli_help_corpus_gate.py``'s module
docstring.
"""

from __future__ import annotations

import re
from functools import cache

from typer.testing import CliRunner

from lode.cli import app

#: Same patterns as tests/test_cli_help_corpus_gate.py -- kept independent
#: rather than imported, since that module's internals (COLUMNS, ALLOWLIST,
#: the panel-parsing helpers) are that file's own concern, not this one's.
_RST_BACKTICK_RE = re.compile(r"``")
_BD_ID_RE = re.compile(r"\blode-[a-z0-9]+(?:\.[0-9]+)?\b")

runner = CliRunner()


@cache
def _top_level_commands_panel() -> str:
    """The ``╭─ Commands ─...─╮`` panel body of bare ``lode --help``, at the
    same pinned COLUMNS=80 width the corpus gate renders at.

    Cached, as the corpus gate caches its own render helper: the output is a
    pure function of the registered command tree, and both tests below want it.
    """
    # NO_COLOR/TERM=dumb: without these, rich colorizes even this non-tty
    # CliRunner capture under GITHUB_ACTIONS=true (rich treats that env var
    # as terminal-capable), and the raw ANSI SGR escapes break the ``╭``
    # panel-border scan below. See tests/test_cli_help_corpus_gate.py's
    # _ANSI_ESCAPE_RE comment for the measured rationale and escape counts.
    result = runner.invoke(
        app, ["--help"], env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0, f"lode --help exited {result.exit_code}"

    lines = result.output.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("╭") and "Commands" in line
    )
    end = next(
        i for i, line in enumerate(lines[start:], start) if line.strip().startswith("╰")
    )
    return "\n".join(lines[start : end + 1])


def test_top_level_help_commands_panel_has_no_bd_ids_or_rst_backticks() -> None:
    panel = _top_level_commands_panel()
    assert not _BD_ID_RE.search(panel), (
        f"lode --help's Commands panel leaks a bd issue id:\n{panel}"
    )
    assert not _RST_BACKTICK_RE.search(panel), (
        f"lode --help's Commands panel leaks RST double-backtick markup:\n{panel}"
    )


def test_top_level_help_commands_panel_is_non_empty() -> None:
    """Non-vacuity: pins that the panel-boundary parsing above actually finds
    real command entries, so a future rendering change that silently breaks
    the ``╭``/``╰`` scan can't make the assertion above vacuously pass. The
    ``models`` row is pinned specifically: it is a sub-app GROUP, the one
    kind of entry the corpus gate's leaf-only walk never sees."""
    panel = _top_level_commands_panel()
    body = panel.splitlines()[1:-1]  # drop the ╭ / ╰ borders
    assert body, f"Commands panel parsed as empty:\n{panel}"
    assert "models" in panel

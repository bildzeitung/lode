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

Deliberately does NOT re-derive the corpus gate's own rule catalogue (no
Sphinx-role/test-name/line-length checks here) -- this file checks only
what lode-ii25.7's acceptance criteria names: bd ids and RST backticks in
the top-level list. Internal-symbol-name and plain-language-quality are a
human judgment call, not something a regex can assert; that read is
recorded in this ticket's ``--design`` field, not encoded here.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from lode.cli import app

#: Same patterns as tests/test_cli_help_corpus_gate.py -- kept independent
#: rather than imported, since that module's internals (COLUMNS, ALLOWLIST,
#: the panel-parsing helpers) are that file's own concern, not this one's.
_RST_BACKTICK_RE = re.compile(r"``")
_BD_ID_RE = re.compile(r"\blode-[a-z0-9]+(?:\.[0-9]+)?\b")

runner = CliRunner()


def _top_level_commands_panel() -> str:
    """The ``╭─ Commands ─...─╮`` panel body of bare ``lode --help``, at the
    same pinned COLUMNS=80 width the corpus gate renders at."""
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "80"})
    assert result.exit_code == 0, f"lode --help exited {result.exit_code}"
    output = result.output

    lines = output.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("╭") and "Commands" in line
    )
    end = next(
        i
        for i, line in enumerate(lines[start:], start)
        if lines[i].strip().startswith("╰")
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
    the ``╭``/``╰`` scan can't make the assertion above vacuously pass."""
    panel = _top_level_commands_panel()
    assert "add" in panel
    assert "models" in panel

"""Transcript-mining baseline for the docs/ lookup index (``lode-dozi``).

Reproduces the retrospective comparison a 2026-09-13 review did by hand:
index invocations (``scripts/docs_index_query.py``, via the Bash tool) vs
read-style DIRECT access to ``docs/*.md`` (grep/sed/head/cat via Bash, the
Read tool, or the Grep tool), split by main session vs subagent
(``isSidechain`` where populated, else the recording session's ``cwd`` -- see
:func:`_is_subagent`), per day. It reads ``~/.claude/projects/**/*.jsonl``
(Claude Code session transcripts) and writes nothing there -- read-only by
design, same as the log this ticket's other half writes
(``scripts/docs_index_log.py``).

The invocation log records the index's OWN use; it cannot show the
comparison against direct grep/read access, because a producer that greps
``docs/*.md`` by hand never touches the index at all. This script is the
only instrument that can measure that comparison, at the cost of parsing an
externally-defined transcript format this repo does not own.

TRANSCRIPT-ENCODING CAVEATS (hit while writing this):

- A ``tool_result`` (and occasionally ``tool_use.input``) is not reliably
  one shape: content can be a plain string, or a list of content blocks
  (``{"type": "text", "text": ...}``). This script only reads ``tool_use``
  blocks, which are consistently a dict with ``name``/``input`` keys in every
  transcript sampled -- it never depends on ``tool_result`` shape at all, so
  that particular inconsistency doesn't reach this script's own parsing.
- Not every JSON line is a message turn (``type`` values like
  ``"last-prompt"``, ``"mode"``, ``"bridge-session"``, or a hook-attachment
  record carry no ``message`` key at all) -- skipped via a plain ``.get``,
  not an assumed schema.
- A line that fails to parse as JSON (a truncated write, a hand-edited file)
  is skipped, not fatal -- consistent with ``docs_index_log.read_log``'s own
  degrade-not-crash rule.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(add_completion=False)

#: Bash command substrings that count as a docs-index invocation vs a
#: direct, read-style access to docs/*.md. Order matters: index check first,
#: since "python scripts/docs_index_query.py ... docs/design.md" (unlikely,
#: but possible in an argument) must not double-count as a direct access too.
_INDEX_SCRIPT_MARKER = "docs_index_query.py"
_DIRECT_BASH_COMMANDS = ("grep", "sed", "head", "cat", "tail", "awk")


def _iter_tool_uses(
    transcript_path: Path,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(entry, tool_use_block)`` pairs for every tool_use block in a
    transcript file. ``entry`` is the outer JSON object (carries
    ``timestamp``/``isSidechain``/``cwd``); ``tool_use_block`` is the inner
    dict with ``name``/``input``."""
    try:
        handle = transcript_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    yield entry, item


def _mentions_docs_md(text: str) -> bool:
    return "docs/" in text and ".md" in text


def _classify(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Return ``"index"``, ``"direct"``, ``"ambiguous"``, or ``None`` (not
    relevant) for one tool_use block.

    ``ambiguous`` exists so an UNNARROWED ``Grep`` -- no path and no glob,
    which defaults to the whole repo and therefore *might* have read docs/ --
    is neither silently dropped nor counted in the headline index-vs-direct
    ratio it would inflate. It is reported on its own line instead.
    """
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if _INDEX_SCRIPT_MARKER in command:
            return "index"
        if _mentions_docs_md(command) and any(
            cmd in command for cmd in _DIRECT_BASH_COMMANDS
        ):
            return "direct"
    elif tool_name == "Read":
        if _mentions_docs_md(str(tool_input.get("file_path", ""))):
            return "direct"
    elif tool_name == "Grep":
        target = str(tool_input.get("path", "")) + str(tool_input.get("glob", ""))
        if "docs" in target:
            return "direct"
        if not target:
            return "ambiguous"
    return None


def _day(timestamp: str) -> str:
    return timestamp[:10] if timestamp else "unknown"


def _is_subagent(entry: dict[str, Any]) -> bool:
    """Whether this tool_use came from a subagent rather than the main session.

    ``isSidechain`` is the direct signal, but it is not always populated (on
    Claude Code 2.1.270 it reads ``false`` on every entry of every transcript
    on the machine this was calibrated against, even though subagents
    demonstrably ran). The ``cwd`` fallback is the SAME main-checkout-vs-
    producer proxy this ticket's invocation log uses: a producer or reviewer
    runs in a ``.claude/worktrees/<hash>`` launch worktree, the main session
    does not. See docs/decisions.md's ``lode-dozi`` entry.
    """
    if entry.get("isSidechain"):
        return True
    return ".claude/worktrees/" in str(entry.get("cwd", ""))


def scan(projects_dir: Path) -> list[dict[str, Any]]:
    """Scan every transcript under ``projects_dir`` and return one row per
    classified tool_use: ``{day, session, is_subagent, kind}``."""
    rows: list[dict[str, Any]] = []
    for transcript_path in sorted(projects_dir.glob("*/*.jsonl")):
        for entry, tool_use in _iter_tool_uses(transcript_path):
            kind = _classify(tool_use.get("name", ""), tool_use.get("input") or {})
            if kind is None:
                continue
            rows.append(
                {
                    "day": _day(str(entry.get("timestamp", ""))),
                    "session": transcript_path.stem,
                    "is_subagent": _is_subagent(entry),
                    "kind": kind,
                }
            )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate `scan()` rows into totals, per-day counts, and
    main-vs-subagent counts, for each of the two kinds."""
    totals: Counter = Counter()
    per_day: defaultdict[str, Counter] = defaultdict(Counter)
    per_role: defaultdict[str, Counter] = defaultdict(Counter)
    for r in rows:
        totals[r["kind"]] += 1
        per_day[r["day"]][r["kind"]] += 1
        per_role["subagent" if r["is_subagent"] else "main"][r["kind"]] += 1
    return {
        "totals": dict(totals),
        "per_day": {day: dict(c) for day, c in sorted(per_day.items())},
        "per_role": {role: dict(c) for role, c in per_role.items()},
    }


def _print_split(label: str, split: dict[str, dict[str, int]]) -> None:
    print()
    print(f"{label}:")
    for key, counts in split.items():
        print(
            f"    {key}: index={counts.get('index', 0)} direct={counts.get('direct', 0)}"
        )


@app.command(
    help=(
        "Mine Claude Code session transcripts for the docs-index-vs-grep "
        "baseline: index invocations vs direct read-style access to "
        "docs/*.md, split by main session vs subagent, per day.\n\nRun this "
        "to reproduce the retrospective comparison the docs-index log alone "
        "cannot show, since a hand grep of docs/*.md never touches the "
        "index. Reads ~/.claude/projects; writes nothing there."
    )
)
def report(
    projects_dir: Annotated[
        Path | None,
        typer.Option(
            "--projects-dir",
            help="Root directory of Claude Code session transcripts.",
        ),
    ] = None,
) -> None:
    """Print the index-vs-direct-access baseline mined from transcripts."""
    resolved_projects_dir = (
        projects_dir
        if projects_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    rows = scan(resolved_projects_dir)
    result = summarize(rows)
    totals = result["totals"]
    index_count = totals.get("index", 0)
    direct_count = totals.get("direct", 0)
    print(f"index invocations: {index_count}")
    print(f"direct docs/*.md access: {direct_count}")
    if index_count + direct_count:
        print(f"index share: {index_count / (index_count + direct_count):.0%}")
    ambiguous_count = totals.get("ambiguous", 0)
    if ambiguous_count:
        print(f"unnarrowed Grep (may or may not have read docs/): {ambiguous_count}")
    _print_split("per day", result["per_day"])
    _print_split("main vs subagent", result["per_role"])


if __name__ == "__main__":
    app()

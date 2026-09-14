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

"Direct" means a READ of ``docs/*.md``, which costs three narrowings --
:func:`_bash_command_names`, :func:`_is_write_or_git_form` and
:func:`project_scope_dirs`, each documented where it lives. Why each was
needed, and what it was worth on real transcripts: docs/decisions.md's
``lode-dozi`` entries.

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
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(add_completion=False)

#: Bash command names that count as a docs-index invocation vs a
#: direct, read-style access to docs/*.md. Order matters: index check first,
#: since "python scripts/docs_index_query.py ... docs/design.md" (unlikely,
#: but possible in an argument) must not double-count as a direct access too.
_INDEX_SCRIPT_MARKER = "docs_index_query.py"
_DIRECT_BASH_COMMANDS = frozenset({"grep", "sed", "head", "cat", "tail", "awk"})

#: A command NAME occupies the start of the command line, or follows a
#: separator (``;`` ``|`` ``&`` ``(`` a newline, a backtick, ``$(``). Anything
#: else that merely spells "cat" or "head" -- most importantly a bd issue body
#: passed as ``--description=...`` -- is a substring, not a command, and must
#: not be counted (lode-dozi: 12 of 176 "direct" rows on the calibration
#: machine were exactly that). A name reached only through an
#: argument-forwarding wrapper (``xargs``, ``sudo``) is missed, which
#: under-counts rather than over-counts -- the direction this fix exists to
#: move, and worth 0 rows of difference on the calibration corpus.
_COMMAND_POSITION_RE = re.compile(
    r"(?:\A|[\n;|&(`]|\$\()\s*(?:[\w./~-]*/)?([A-Za-z][\w.-]*)"
)

#: Forms that WRITE a docs file (or ask git about one) rather than read it.
#: The criterion is "direct" == a READ of docs/*.md, so each of these
#: disqualifies the whole command, not just one token of it. The git clause
#: is NOT subsumed by the command-name allowlist below: `git` alone never
#: matches that allowlist, but `git show <ref>:docs/design.md | grep ...` does,
#: and it decides 40 rows on the calibration corpus.
_REDIRECT_TO_DOCS_RE = re.compile(r">>?\s*\S*docs/\S*\.md")
_SED_INPLACE_RE = re.compile(r"\bsed\b[^|;&\n]*(?:-[A-Za-z]*i\b|--in-place)")
_GIT_NON_READ_RE = re.compile(r"\bgit\s+(?:commit|add|show|diff|log|mv|rm|checkout)\b")


def _bash_command_names(command: str) -> set[str]:
    """The set of command names actually INVOKED by a Bash command string.

    Command-position matching, never a substring scan over the whole line --
    see :data:`_COMMAND_POSITION_RE`.
    """
    return set(_COMMAND_POSITION_RE.findall(command))


def _is_write_or_git_form(command: str) -> bool:
    """Whether this command writes a docs file or is a git operation naming
    one, rather than reading it: a redirect into ``docs/*.md``, a heredoc, an
    in-place ``sed``, a ``tee``, or ``git commit/add/show/diff/log/mv/rm/
    checkout``."""
    return bool(
        _REDIRECT_TO_DOCS_RE.search(command)
        or "<<" in command
        or _SED_INPLACE_RE.search(command)
        or "tee" in _bash_command_names(command)
        or _GIT_NON_READ_RE.search(command)
    )


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
            # A strict prefilter: any line carrying a tool_use block spells
            # the literal. Skipping the rest unparsed is the difference
            # between reading and json.loads-ing multi-MB transcripts whose
            # tool_use turns are a small minority.
            if not line or '"tool_use"' not in line:
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
        if not _mentions_docs_md(command):
            return None
        if not _bash_command_names(command) & _DIRECT_BASH_COMMANDS:
            return None
        if _is_write_or_git_form(command):
            return None
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


#: The path segment that marks a producer/reviewer launch worktree. One
#: spelling, used both to tell a subagent's cwd from the main checkout's and
#: to normalize a worktree cwd back to its project root.
_LAUNCH_WORKTREE_MARKER = "/.claude/worktrees/"


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
    return _LAUNCH_WORKTREE_MARKER in str(entry.get("cwd", ""))


def encode_project_dir_name(path: Path) -> str:
    """Encode a project path the way Claude Code names its transcript
    directory under ``~/.claude/projects`` -- every non-alphanumeric
    character becomes ``-`` (so ``/home/u/PROJECTS/lode`` becomes
    ``-home-u-PROJECTS-lode``, and a worktree's ``.claude`` becomes
    ``-claude``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def project_scope_dirs(projects_dir: Path, cwd: Path) -> list[Path]:
    """The transcript directories belonging to the project containing ``cwd``.

    A session started inside ``.claude/worktrees/<name>`` gets its OWN
    transcript directory, named for the worktree path, so the scope is the
    project root's directory plus every worktree directory derived from it.
    ``cwd`` itself is normalized back to the project root first, so running
    this script from a producer's worktree still scopes to the whole project
    rather than to that one worktree.
    """
    root = str(cwd.resolve()).split(_LAUNCH_WORKTREE_MARKER)[0]
    encoded = encode_project_dir_name(Path(root))
    return [
        child
        for child in projects_dir.glob("*")
        if child.is_dir()
        and (child.name == encoded or child.name.startswith(encoded + "--"))
    ]


def scan(projects_dir: Path) -> list[dict[str, Any]]:
    """Scan every transcript under ``projects_dir`` (at any depth) and return
    one row per classified tool_use: ``{day, session, is_subagent, kind}``."""
    rows: list[dict[str, Any]] = []
    for transcript_path in sorted(projects_dir.rglob("*.jsonl")):
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
            f"    {key}: index={counts.get('index', 0)} "
            f"direct={counts.get('direct', 0)} "
            f"ambiguous={counts.get('ambiguous', 0)}"
        )


@app.command(
    help=(
        "Mine Claude Code session transcripts for the docs-index-vs-grep "
        "baseline: index invocations vs direct read-style access to "
        "docs/*.md, split by main session vs subagent, per day.\n\nRun this "
        "to reproduce the retrospective comparison the docs-index log alone "
        "cannot show, since a hand grep of docs/*.md never touches the "
        "index. Reads ~/.claude/projects; writes nothing there.\n\nScoped to "
        "the current project's transcripts by default; pass --all-projects "
        "to widen to every project on the machine."
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
    all_projects: Annotated[
        bool,
        typer.Option(
            "--all-projects",
            help="Scan every project's transcripts, not just this project's.",
        ),
    ] = False,
) -> None:
    """Print the index-vs-direct-access baseline mined from transcripts."""
    resolved_projects_dir = (
        projects_dir
        if projects_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    roots = (
        [resolved_projects_dir]
        if all_projects
        else project_scope_dirs(resolved_projects_dir, Path.cwd())
    )
    if not roots:
        print(
            "no transcripts for this project under "
            f"{resolved_projects_dir} -- pass --all-projects to widen"
        )
    rows = [row for root in roots for row in scan(root)]
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

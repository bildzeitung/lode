"""Per-lookup cost of decisions-record lookups, mined from session transcripts (lode-ebayj.1).

Builds on ``docs_index_usage_report``: the same transcript scope and the same
Bash segment rules, plus the size of each call's ``tool_result``. Bytes are
the ``tool_result`` text size, which is what lands in the model's context;
tokens are estimated as bytes / 4.

- The *decisions record* is ``docs/decisions.md`` or any file under
  ``docs/decisions/``, so one command measures the single-file layout and the
  per-entry layout alike.
- A *lookup* is a ``docs_index_query.py`` call whose output has a pointer row
  (``<path>:<lo>-<hi>``) into the record. A snippet that merely mentions the
  record is not one.
- A *read* is a direct read of the record: the Read tool, a Grep tool call
  scoped to it, or a grep/sed/head/cat/tail/awk Bash segment that names it
  and does not write it. A *follow-up* read comes after a lookup in the same
  session, or shares its call.
- A *session* is one transcript file, so a subagent is its own session: each
  has its own context.

A Bash result is one text for the whole command, so the size of a lookup or a
read is taken from *sole* calls only: a Read or Grep tool call, or a Bash
command that is one pipeline (:data:`_COMPOUND_RE`), whose whole output is
that one lookup or read. Counts cover every call. Per-session bytes count a
compound command's whole output, which makes them an upper bound.

Not counted: a read that reaches the record without naming it, through a glob
or a parent directory (``grep -rn x docs/*.md``, ``grep -rn x docs/``).

Read-only and offline: reads ``~/.claude/projects``, writes nothing, calls no
provider.
"""

from __future__ import annotations

import importlib.util
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(add_completion=False)

_spec = importlib.util.spec_from_file_location(
    "docs_index_usage_report", Path(__file__).with_name("docs_index_usage_report.py")
)
assert _spec is not None and _spec.loader is not None
_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_usage)

_DECISIONS_RE = re.compile(r"docs/decisions(?:\.md|/)")

#: The index prints the path of the checkout that ran the query, so a pointer
#: row starts with an absolute path.
_POINTER_RE = re.compile(r"^\S*docs/decisions(?:\.md|/\S+\.md):\d+-\d+", re.MULTILINE)

#: What makes a Bash command more than one pipeline. Matched against the raw
#: command, so a separator inside a quoted argument also counts: that call is
#: left out of the size figures, never sized wrongly.
_COMPOUND_RE = re.compile(r";|&&|\|\||\n")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _results_by_id(transcript_path: Path) -> dict[str, str]:
    """Map tool_use_id -> tool_result text for one transcript."""
    out: dict[str, str] = {}
    try:
        handle = transcript_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return out
    with handle:
        for line in handle:
            if '"tool_result"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tid = item.get("tool_use_id")
                    if isinstance(tid, str):
                        out[tid] = _result_text(item.get("content"))
    return out


def _kinds(name: str, tool_input: dict[str, Any], text: str) -> tuple[bool, bool]:
    """``(lookup, read)`` for one tool call and its result text. A Bash
    command can be both: ``docs_index_query.py x; sed -n 1,9p docs/decisions.md``."""
    if name == "Read":
        return False, bool(_DECISIONS_RE.search(str(tool_input.get("file_path", ""))))
    if name == "Grep":
        path = str(tool_input.get("path", "")).rstrip("/")
        return False, bool(_DECISIONS_RE.search(f"{path}/{tool_input.get('glob', '')}"))
    if name != "Bash":
        return False, False
    command = str(tool_input.get("command", ""))
    cited = _usage._INDEX_SCRIPT_MARKER in command and bool(_POINTER_RE.search(text))
    if not cited and not _DECISIONS_RE.search(command):
        return False, False
    segments = _usage._split_segments(command)
    lookup = cited and any(_usage._is_index_segment(s) for s in segments)
    read = any(
        bool(_DECISIONS_RE.search(s))
        and _usage._segment_command_name(s) in _usage._DIRECT_BASH_COMMANDS
        and not _usage._is_write_form(s)
        for s in segments
    )
    return lookup, read


def scan(*roots: Path) -> list[dict[str, Any]]:
    """One row per tool call that looks the decisions record up or reads it:
    ``{day, session, lookup, read, followup, sole, bytes, lines}``. A tool_use
    id counts once, and takes its day from whichever copy carries a timestamp."""
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(p for d in roots for p in d.rglob("*.jsonl")):
        results = _results_by_id(path)
        looked_up = False
        for entry, tool_use in _usage._iter_tool_uses(path):
            tid = tool_use.get("id")
            if not isinstance(tid, str) or tid not in results:
                continue
            day = str(entry.get("timestamp", ""))[:10]
            held = rows.get(tid)
            if held is not None:
                held["day"] = held["day"] or day
                continue
            name = tool_use.get("name", "")
            tool_input = tool_use.get("input") or {}
            text = results[tid]
            lookup, read = _kinds(name, tool_input, text)
            if not lookup and not read:
                continue
            looked_up = looked_up or lookup
            one_pipeline = name != "Bash" or not _COMPOUND_RE.search(
                str(tool_input.get("command", ""))
            )
            # A filter on the index's own output (``| grep docs/decisions.md``)
            # names the record without reading it.
            read = read and not (lookup and one_pipeline)
            rows[tid] = {
                "day": day,
                "session": path.stem,
                "lookup": lookup,
                "read": read,
                "followup": read and looked_up,
                "sole": one_pipeline,
                "bytes": len(text.encode("utf-8")),
                "lines": len(text.splitlines()),
            }
    return list(rows.values())


def _dist(values: list[int]) -> str:
    if not values:
        return "n=0"
    ordered = sorted(values)
    # Nearest rank: the smallest value with at least 90% of the sample at or below it.
    p90 = ordered[(9 * len(ordered) + 9) // 10 - 1]
    return (
        f"n={len(values)} mean={statistics.mean(values):.0f} "
        f"median={statistics.median(values):.0f} p90={p90} max={ordered[-1]}"
    )


def summarize(rows: list[dict[str, Any]]) -> list[str]:
    lookups = [r for r in rows if r["lookup"]]
    reads = [r for r in rows if r["read"]]
    followups = sum(r["followup"] for r in rows)
    sessions: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"lookups": 0, "followups": 0, "bytes": 0}
    )
    for r in rows:
        s = sessions[r["session"]]
        s["lookups"] += r["lookup"]
        s["followups"] += r["followup"]
        if r["lookup"] or r["followup"]:
            s["bytes"] += r["bytes"]
    with_lookup = [s for s in sessions.values() if s["lookups"]]
    out = [
        f"decisions lookups (index calls with a decisions pointer row): {len(lookups)}",
        f"  output bytes per lookup, sole calls: {_dist([r['bytes'] for r in lookups if r['sole']])}",
        f"decisions reads: {len(reads)} (follow-up to a lookup: {followups})",
        f"  result bytes per read, sole calls: {_dist([r['bytes'] for r in reads if r['sole']])}",
        f"  result lines per read, sole calls: {_dist([r['lines'] for r in reads if r['sole']])}",
        f"sessions with >=1 lookup: {len(with_lookup)} (of {len(sessions)} touching decisions)",
        f"  lookups per such session: {_dist([s['lookups'] for s in with_lookup])}",
        f"  follow-up reads per such session: {_dist([s['followups'] for s in with_lookup])}",
        f"  lookup + follow-up bytes per such session (upper bound): {_dist([s['bytes'] for s in with_lookup])}",
    ]
    if lookups:
        per_lookup = sum(s["bytes"] for s in with_lookup) / len(lookups)
        out.append(
            f"bytes per lookup incl. follow-up reads (upper bound): {per_lookup:.0f} "
            f"(~{per_lookup / 4:.0f} tokens)"
        )
    return out


@app.command(help="Print decisions-record lookup cost mined from session transcripts.")
def report(
    projects_dir: Annotated[
        Path | None,
        typer.Option(
            "--projects-dir", help="Root directory of Claude Code session transcripts."
        ),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all-projects", help="Scan every project's transcripts."),
    ] = False,
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help="Count only calls made on or after this UTC day.",
        ),
    ] = None,
) -> None:
    root = (
        projects_dir
        if projects_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    roots = [root] if all_projects else _usage.project_scope_dirs(root, Path.cwd())
    if not roots:
        print(
            f"no transcripts for this project under {root} -- pass --all-projects to widen"
        )
    rows = scan(*roots)
    if since is not None:
        rows = [r for r in rows if r["day"] >= since.date().isoformat()]
    for line in summarize(rows):
        print(line)


if __name__ == "__main__":
    app()

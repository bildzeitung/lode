"""Per-lookup cost of docs/decisions.md lookups, mined from session transcripts (lode-ebayj.1).

Builds on ``docs_index_usage_report``: the same transcript scope, the same
index/direct classification, plus the size of each call's ``tool_result``.
A *lookup* is a ``docs_index_query.py`` call whose output cites
``docs/decisions.md``; a *follow-up read* is a direct read of
``decisions.md`` (Read tool, or grep/sed/head/cat/tail/awk via Bash) in the
same session. Bytes are the ``tool_result`` text size, which is what lands in
the model's context; tokens are estimated as bytes / 4.

Read-only and offline: reads ``~/.claude/projects``, writes nothing, calls no
provider.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
from collections import defaultdict
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

_DECISIONS = "decisions.md"


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


def _touches_decisions(tool_name: str, tool_input: dict[str, Any]) -> bool:
    if tool_name == "Bash":
        return _DECISIONS in str(tool_input.get("command", ""))
    if tool_name == "Read":
        return str(tool_input.get("file_path", "")).endswith(_DECISIONS)
    if tool_name == "Grep":
        return _DECISIONS in str(tool_input.get("path", "")) + str(
            tool_input.get("glob", "")
        )
    return False


def _iter_results(transcript_path: Path) -> dict[str, str]:
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


def scan(*roots: Path) -> list[dict[str, Any]]:
    """One row per decisions.md lookup or follow-up read (a tool_use id counts once)."""
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(p for d in roots for p in d.rglob("*.jsonl")):
        results = _iter_results(path)
        for entry, tool_use in _usage._iter_tool_uses(path):
            name = tool_use.get("name", "")
            tool_input = tool_use.get("input") or {}
            kind = _usage._classify(name, tool_input)
            tid = tool_use.get("id")
            if (
                kind not in ("index", "direct")
                or not isinstance(tid, str)
                or tid not in results
            ):
                continue
            text = results[tid]
            if kind == "index":
                if "docs/decisions.md" not in text:
                    continue
                row_kind = "lookup"
            elif _touches_decisions(name, tool_input):
                row_kind = "read"
            else:
                continue
            rows.setdefault(
                tid,
                {
                    "session": path.stem,
                    "subagent": _usage._is_subagent(entry),
                    "kind": row_kind,
                    "bytes": len(text.encode("utf-8")),
                    "lines": text.count("\n") + 1 if text else 0,
                },
            )
    return list(rows.values())


def _dist(values: list[int]) -> str:
    if not values:
        return "n=0"
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return (
        f"n={len(values)} mean={statistics.mean(values):.0f} "
        f"median={statistics.median(values):.0f} p90={p90} max={ordered[-1]}"
    )


def summarize(rows: list[dict[str, Any]]) -> list[str]:
    lookups = [r for r in rows if r["kind"] == "lookup"]
    reads = [r for r in rows if r["kind"] == "read"]
    sessions = defaultdict(lambda: {"lookup": 0, "read": 0, "bytes": 0})
    for r in rows:
        s = sessions[r["session"]]
        s[r["kind"]] += 1
        s["bytes"] += r["bytes"]
    with_lookup = [s for s in sessions.values() if s["lookup"]]
    out = [
        f"decisions.md lookups (index output citing it): {len(lookups)}",
        f"  output bytes per lookup: {_dist([r['bytes'] for r in lookups])}",
        f"decisions.md follow-up/direct reads: {len(reads)}",
        f"  result bytes per read:  {_dist([r['bytes'] for r in reads])}",
        f"  result lines per read:  {_dist([r['lines'] for r in reads])}",
        f"sessions with >=1 lookup: {len(with_lookup)} (of {len(sessions)} touching decisions.md)",
        f"  lookups per such session: {_dist([s['lookup'] for s in with_lookup])}",
        f"  reads per such session:   {_dist([s['read'] for s in with_lookup])}",
        f"  decisions bytes into context per such session: {_dist([s['bytes'] for s in with_lookup])}",
    ]
    if lookups:
        total = sum(
            r["bytes"]
            for r in rows
            if r["session"] in {r2["session"] for r2 in lookups}
        )
        out.append(
            f"bytes per lookup incl. same-session reads: {total / len(lookups):.0f} "
            f"(~{total / len(lookups) / 4:.0f} tokens)"
        )
    return out


@app.command(help="Print decisions.md lookup cost mined from session transcripts.")
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
) -> None:
    root = (
        projects_dir
        if projects_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    roots = [root] if all_projects else _usage.project_scope_dirs(root, Path.cwd())
    for line in summarize(scan(*roots)):
        print(line)


if __name__ == "__main__":
    app()

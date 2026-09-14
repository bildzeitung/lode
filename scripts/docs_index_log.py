"""Invocation log for the docs/ lookup index (``lode-dozi``).

Every run of ``scripts/docs_index_query.py`` appends one JSON line here via
:func:`append_invocation`, so whether the index earns its place does not
depend on hand-mining session transcripts (that's a separate, coarser
instrument: ``scripts/docs_index_usage_report.py``, ``lode-dozi``'s second
half). Log location follows the SAME fallback rule as the index's own build
target -- :func:`docs_index_build.cache_db_path` -- so it lands outside the
repo worktree for the identical reason: nothing here may ever become a
tracked file. See ``docs/decisions.md``'s ``lode-dozi`` entry for the
field list and rationale.

This module is deliberately independent of ``docs_index_query.py``'s own
logic (query escaping, FTS5, ranking) -- it only appends and later
summarizes JSON lines. Keeping it separate means the query CLI's own diff for
this ticket is a single import plus a single call at its one exit point,
which matters because a sibling ticket (lode-qcp0) is editing
``docs_index_query.py`` concurrently for an unrelated reason (an AND/OR
zero-hit fallback).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(add_completion=False)


def _load_build():
    """Load scripts/docs_index_build.py under a PRIVATE sys.modules name, via
    the shared scripts/docs_index_loader.py bootstrap (``lode-wtk2``).

    ``log_path`` used to hand-duplicate ``docs_index_build``'s XDG fallback
    rule rather than import it, specifically to avoid a SECOND, independent
    load path for that module (two independent loads would create two
    distinct module objects -- see ``docs_index_query.py``'s own
    ``_load_build`` docstring). Going through the shared, cached
    ``load_sibling`` here removes that risk: any caller asking for
    ``docs_index_build`` under its own private name gets the SAME cached
    module object back, never a second one.
    """
    name = "_docs_index_log_loader_impl"
    if name in sys.modules:
        loader = sys.modules[name]
    else:
        path = Path(__file__).resolve().parent / "docs_index_loader.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        loader = importlib.util.module_from_spec(spec)
        sys.modules[name] = loader
        spec.loader.exec_module(loader)
    return loader.load_sibling("_docs_index_log_build_impl", "docs_index_build.py")


def log_path() -> Path:
    """The invocation log location: ``$XDG_CACHE_HOME/lode/docs-index-log.jsonl``
    if ``XDG_CACHE_HOME`` is set to an absolute path, else
    ``~/.cache/lode/docs-index-log.jsonl`` -- outside the repo worktree,
    same as the index db itself (see module docstring)."""
    return _load_build().cache_dir() / "docs-index-log.jsonl"


#: Env vars that identify the calling harness/session, when present. Values
#: are copied into the log entry verbatim if set; this is deliberately a
#: small, fixed allowlist -- never a full os.environ dump, which would leak
#: unrelated secrets into a cache-dir file with no access controls.
_HARNESS_ENV_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
)


def _harness_context() -> dict[str, str]:
    return {k: v for k in _HARNESS_ENV_VARS if (v := os.environ.get(k)) is not None}


def append_invocation(
    *,
    query_text: str,
    hit_count: int,
    fallback_fired: bool,
    elapsed_ms: float,
    cwd: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one JSON line describing an index invocation.

    ``fallback_fired`` records whether a zero-hit AND->OR fallback fired --
    that fallback does not exist in this diff (a sibling ticket, lode-qcp0,
    is adding it concurrently); callers without one pass ``False`` always,
    and the field is here so this log format does not need a second schema
    change once that fallback lands.
    """
    resolved = path if path is not None else log_path()
    entry: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": query_text,
        "hit_count": hit_count,
        "fallback_fired": fallback_fired,
        "elapsed_ms": elapsed_ms,
        "cwd": cwd if cwd is not None else str(Path.cwd()),
    }
    entry.update(_harness_context())
    line = json.dumps(entry) + "\n"
    # Create the cache dir only when it is actually missing -- this runs on
    # the path CLAUDE.md routes every agent through, and after the first run
    # ever the directory is always there.
    try:
        with resolved.open("a", encoding="utf-8") as f:
            f.write(line)
    except FileNotFoundError:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("a", encoding="utf-8") as f:
            f.write(line)


def read_log(path: Path | None = None) -> list[dict[str, Any]]:
    """Read every entry from the log, skipping unparseable lines (a log that
    was mid-write when read, or hand-edited, should degrade, not crash)."""
    resolved = path if path is not None else log_path()
    if not resolved.exists():
        return []
    entries = []
    # Streamed, not slurped: this log is append-only and grows by one line
    # per docs lookup from every agent, forever.
    with resolved.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def compute_stats(entries: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    """Summarize invocation log entries: total count, miss rate (hit_count
    == 0), and the top zero-hit queries by frequency."""
    total = len(entries)
    zero_hit_counts = Counter(
        e.get("query", "") for e in entries if e.get("hit_count") == 0
    )
    misses = sum(zero_hit_counts.values())
    # Ties broken by query text, so the output is stable across runs --
    # Counter.most_common() ties by insertion order instead.
    top_zero_hit = sorted(zero_hit_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return {
        "invocations": total,
        "misses": misses,
        "miss_rate": (misses / total) if total else 0.0,
        "top_zero_hit_queries": top_zero_hit,
    }


@app.command(
    help=(
        "Summarize the docs-index invocation log: total invocations, miss "
        "rate, and the top zero-hit queries.\n\nRun this to see whether the "
        "docs index is earning its place -- how often it's used and how "
        "often it comes back empty."
    )
)
def stats(
    log_file: Annotated[
        Path | None,
        typer.Option("--log-path", help="Override the log file location."),
    ] = None,
    top: Annotated[
        int,
        typer.Option("--top", min=1, help="How many top zero-hit queries to print."),
    ] = 5,
) -> None:
    """Print invocation count, miss rate, and top zero-hit queries."""
    entries = read_log(log_file)
    result = compute_stats(entries, top=top)
    if result["invocations"] == 0:
        print("No invocations logged.")
        return
    print(f"invocations: {result['invocations']}")
    print(f"misses: {result['misses']} ({result['miss_rate']:.0%})")
    if result["top_zero_hit_queries"]:
        print("top zero-hit queries:")
        for q, count in result["top_zero_hit_queries"]:
            print(f"    {count:>4}  {q}")


if __name__ == "__main__":
    app()

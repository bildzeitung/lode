"""Edge gate for the per-entry decision files under ``docs/decisions/`` (lode-ebayj.2).

Fails (exit 1) on: a duplicate id, an id/filename mismatch, a ``supersedes`` or
``related`` target that does not exist, a ``supersedes`` cycle, and any
modification or deletion of an entry file that exists at the base ref. Exit 2
means the gate could not run (a base ref that does not resolve) and is never a
verdict on the entries. The format is defined in ``docs/decisions/README.md``.

:func:`chain_head` is shared with ``docs_index_query.py``, which loads this
module as a sibling to name the head of a superseded hit's chain.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "docs" / "decisions"
_NAME_RE = re.compile(r"^([0-9a-z]{5})-.+\.md$")
_LIST_RE = re.compile(r"^\[(.*)\]$")


def parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """The ``key: value`` lines between the leading ``---`` fences; ``[a, b]``
    values become lists. A file with no frontmatter yields ``{}``."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str | list[str]] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        m = _LIST_RE.match(value)
        meta[key.strip()] = (
            [v.strip() for v in m.group(1).split(",") if v.strip()] if m else value
        )
    return meta


def _ids(meta: dict[str, str | list[str]], key: str) -> list[str]:
    value = meta.get(key, [])
    return value if isinstance(value, list) else [value]


def load_entries(directory: Path) -> tuple[dict[str, dict], list[str]]:
    """``{id: {"path", "date", "supersedes", "related"}}`` plus the problems
    found while loading (filename mismatch, duplicate id)."""
    entries: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        name_id = m.group(1) if (m := _NAME_RE.match(path.name)) else None
        entry_id = meta.get("id")
        if name_id is None or entry_id != name_id:
            problems.append(f"{path.name}: id {entry_id!r} does not match filename")
            continue
        if entry_id in entries:
            problems.append(f"{path.name}: duplicate id {entry_id}")
            continue
        entries[entry_id] = {
            "path": path,
            "date": str(meta.get("date", "")),
            "supersedes": _ids(meta, "supersedes"),
            "related": _ids(meta, "related"),
        }
    return entries, problems


def successors(entries: dict[str, dict]) -> dict[str, list[str]]:
    """The derived ``superseded_by`` map: old id -> the ids that supersede it."""
    out: dict[str, list[str]] = {}
    for entry_id, entry in entries.items():
        for old in entry["supersedes"]:
            out.setdefault(old, []).append(entry_id)
    return out


def chain_head(entries: dict[str, dict], entry_id: str) -> str:
    """Follow ``superseded_by`` from ``entry_id`` to an entry nothing supersedes.
    With several successors, the newest by ``(date, id)`` wins. Stops on a cycle."""
    nxt, seen = successors(entries), {entry_id}
    while entry_id in nxt:
        entry_id = max(nxt[entry_id], key=lambda i: (entries[i]["date"], i))
        if entry_id in seen:
            break
        seen.add(entry_id)
    return entry_id


def _has_cycle(entries: dict[str, dict]) -> bool:
    state: dict[str, int] = {}  # 1 = on the DFS stack, 2 = done

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2 or node not in entries:
            return False
        state[node] = 1
        found = any(visit(old) for old in entries[node]["supersedes"])
        state[node] = 2
        return found

    return any(visit(node) for node in entries)


def check_edges(directory: Path) -> list[str]:
    entries, problems = load_entries(directory)
    for entry in entries.values():
        for key in ("supersedes", "related"):
            problems += [
                f"{entry['path'].name}: {key} target {t} does not exist"
                for t in entry[key]
                if t not in entries
            ]
    if _has_cycle(entries):
        problems.append("supersedes cycle")
    return problems


def check_immutable(directory: Path, base: str) -> list[str]:
    """Entry files that exist at ``base`` must be byte-identical at the worktree."""
    git = ["git", "-C", str(directory)]
    if subprocess.run(
        [*git, "rev-parse", "--verify", base], capture_output=True, check=False
    ).returncode:
        print(
            f"GATE COULD NOT RUN: base ref {base!r} does not resolve", file=sys.stderr
        )
        raise typer.Exit(2)
    diff = subprocess.run(
        [*git, "diff", "--name-status", "--no-renames", base, "--", "."],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = [row.split("\t") for row in diff.splitlines()]
    return [
        f"{Path(f).name}: committed entry was {'deleted' if s == 'D' else 'modified'}"
        for s, f in rows
        if s != "A" and f.endswith(".md") and Path(f).name != "README.md"
    ]


def main(
    directory: Annotated[Path, typer.Option(help="Entry directory.")] = DEFAULT_DIR,
    base: Annotated[
        str, typer.Option(help="Ref whose entries are frozen.")
    ] = "origin/trunk",
) -> None:
    """Check the decision entries' edges and that committed entries are unchanged."""
    problems: list[str] = []
    if directory.is_dir():
        problems = check_edges(directory) + check_immutable(directory, base)
    for problem in problems:
        print(problem)
    raise typer.Exit(1 if problems else 0)


if __name__ == "__main__":
    typer.run(main)

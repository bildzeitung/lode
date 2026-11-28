"""Edge gate for the per-entry decision files under ``docs/decisions/`` (lode-ebayj.2).

Fails (exit 1) on: a duplicate id, an id/filename mismatch, a ``supersedes`` or
``related`` value that is not an inline list or names a target that does not
exist, a ``supersedes`` cycle, and any modification or deletion of an entry
file that exists where ``HEAD`` forked from the base ref. Exit 2 means the gate
could not run (no merge base with the base ref) and is never a verdict on the
entries. The format is defined in ``docs/decisions/README.md``.

:func:`chain_head` is shared with ``docs_index_query.py``, which loads this
module as a sibling to name the head of a superseded hit's chain.
"""

from __future__ import annotations

import graphlib
import re
import subprocess
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)

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


def load_entries(directory: Path) -> tuple[dict[str, dict], list[str]]:
    """``{id: {"path", "date", "supersedes", "related"}}`` plus the problems
    found while loading (filename mismatch, duplicate id, an edge value that
    is not an inline list)."""
    entries: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        name = _NAME_RE.match(path.name)
        entry_id = meta.get("id")
        if not name or entry_id != name.group(1):
            problems.append(f"{path.name}: id {entry_id!r} does not match filename")
            continue
        if entry_id in entries:
            problems.append(f"{path.name}: duplicate id {entry_id}")
            continue
        entries[entry_id] = {"path": path, "date": str(meta.get("date", ""))}
        for key in ("supersedes", "related"):
            value = meta.get(key, [])
            if not isinstance(value, list):
                # A blank value can be a YAML block list this parser does not
                # read: failing here keeps its edges from being dropped unseen.
                problems.append(f"{path.name}: {key} must be an inline [id, ...] list")
                value = []
            entries[entry_id][key] = value
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


def check_edges(directory: Path) -> list[str]:
    entries, problems = load_entries(directory)
    for entry in entries.values():
        for key in ("supersedes", "related"):
            problems += [
                f"{entry['path'].name}: {key} target {t} does not exist"
                for t in entry[key]
                if t not in entries
            ]
    graph = {entry_id: entry["supersedes"] for entry_id, entry in entries.items()}
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        problems.append(f"supersedes cycle: {' -> '.join(exc.args[1])}")
    return problems


def check_immutable(directory: Path, base: str) -> list[str]:
    """Entry files that exist where ``HEAD`` forked from ``base`` must be
    byte-identical in the worktree.

    The merge base, not ``base``'s tip: a branch that is behind ``base`` lacks
    the entries ``base`` has gained since, and each would read as deleted.
    """
    git = ["git", "-C", str(directory)]
    fork = subprocess.run(
        [*git, "merge-base", base, "HEAD"], capture_output=True, text=True, check=False
    )
    if fork.returncode:
        typer.echo(f"GATE COULD NOT RUN: no merge base of {base!r} and HEAD", err=True)
        raise typer.Exit(2)
    # ``-z``: without it git quotes a non-ASCII path, which would then fail
    # the ``.md`` test below and go unchecked.
    diff = [*git, "diff", "-z", "--name-status", "--no-renames", fork.stdout.strip()]
    fields = subprocess.run(
        [*diff, "--", "."], capture_output=True, text=True, check=True
    ).stdout.split("\0")
    return [
        f"{Path(f).name}: committed entry was {'deleted' if s == 'D' else 'modified'}"
        for s, f in zip(fields[::2], fields[1::2])
        if s != "A" and f.endswith(".md") and Path(f).name != "README.md"
    ]


@app.command()
def main(
    directory: Annotated[Path, typer.Option(help="Entry directory.")] = DEFAULT_DIR,
    base: Annotated[
        str, typer.Option(help="Ref HEAD forked from; entries at the fork are frozen.")
    ] = "origin/trunk",
) -> None:
    """Check the decision entries' edges and that committed entries are unchanged."""
    if not directory.is_dir():
        return
    edges = check_edges(directory)
    # Printed before the immutability check runs: its exit 2 must not discard them.
    print(*edges, sep="\n", end="\n" if edges else "")
    frozen = check_immutable(directory, base)
    print(*frozen, sep="\n", end="\n" if frozen else "")
    raise typer.Exit(1 if edges or frozen else 0)


if __name__ == "__main__":
    app()

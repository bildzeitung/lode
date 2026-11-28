"""Dry-run migration of ``docs/decisions.md`` into one file per entry (lode-ebayj.3).

Splits the single-file log into ``docs/decisions/<id>-<slug>.md`` entry files
(format: ``docs/decisions/README.md``) in a TARGET directory given on the
command line. It reads the source and writes only the target, the mapping file
and its report on stdout; the cutover ticket (lode-ebayj.4) is what puts the
result into ``docs/``. The target must not exist or must be empty, so a run can
never overwrite ``docs/decisions/``.

An entry is a column-zero ``- **`` bullet or a ``## `` section outside a code
fence; it runs to the next entry. A section's entry also holds the column-zero
bullets after it, its sub-points, up to the next section or the first bullet
named in :data:`NEW_ENTRY_BULLETS`. Everything before the first entry (the
preamble, superseded by the directory README) is not migrated. An entry's body
is its source lines VERBATIM, trailing blank lines included, so the bodies
concatenated in source order are the source minus the preamble, byte for byte.
The only edits are the reviewed :data:`OVERRIDES`: a marker block moved to the
entry it corrects, or dropped as obsolete. :func:`verify` re-reads the written
files and checks that, so a run that prints ``VERIFY: ok`` has proved the
counts and the bytes.

The report lists every bullet merged into a section, for the reviewer to accept
or to name in :data:`NEW_ENTRY_BULLETS`.

Frontmatter, derived mechanically. The report lists every date not taken from a
title and every status derived from a title with open wording:

- ``id``: random, 5 lowercase base36 characters, unique within the run
  (``--seed`` makes a run repeatable).
- ``date``: the first ISO date in the entry's title, else in its first
  paragraph, else anywhere in the entry, else the nearest dated entry's date.
- ``status``: ``decided`` or ``open`` from the title's wording; an Update
  marker that reads as a ruling turns an open entry decided. Both kinds of
  wording in one title, or an open title with Updates none of which reads as a
  ruling, is UNCLEAR: it gets ``open`` and is listed.
- ``supersedes`` and ``related`` are empty: the migration invents no edges.

Also written: a mapping file (JSON) from every old heading anchor and every
bullet title, a section's sub-points included, to the new file, for the link
rewiring in lode-ebayj.4.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Annotated

import typer

from lode.docs_slug import anchor_slugs, github_slug
from lode.fence_parsing import fence_flags

app = typer.Typer(add_completion=False)

_ENTRY_START_RE = re.compile(r"^(?:- \*\*|## )")
_MARKER_RE = re.compile(r"\*\*Update \(")
_MARKER_LINE_RE = re.compile(r"^\s*(?:- )?\*\*Update \(")
_MARKER_ID_RE = re.compile(r"\*\*Update \(`?(lode-[\w.]*\w)")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_ID_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"
_SLUG_MAX = 60

# Status wording. A ruling marker is looked for in the 250 characters after an
# Update's lead-in; deferred is deliberately in neither set (a decision to defer
# is a decision).
_OPEN_RE = re.compile(r"\b(open|leaning|undecided|unsettled)\b", re.IGNORECASE)
_DECIDED_RE = re.compile(
    r"\b(decided|decision|decisions|settled|accepted|ruled|ruling|resolved|built|"
    r"landed|done|rejected|withdrawn|fixed|closed|chosen|verified|approved)\b",
    re.IGNORECASE,
)
_RULING_RE = re.compile(
    r"\b(decided|settled|ruled|approved|resolved|accepted|built|landed|fixed|closed)\b",
    re.IGNORECASE,
)

# Suspect heuristics for a marker that may sit under the wrong entry.
_MISFILED_RE = re.compile(
    r"does not belong|belongs (?:to|under|in)\b|misfiled|by mistake|wrong (?:entry|section)",
    re.IGNORECASE,
)
_POINTS_ELSEWHERE_RE = re.compile(
    r"(?:entry|section) (?:above|below|at the end of this file|elsewhere)",
    re.IGNORECASE,
)
_NAMED_ENTRY_RE = re.compile(r"`?(lode-[\w.]*\w)`? (?:entry|section)")


@dataclass(frozen=True)
class Override:
    """A reviewed correction to where one marker block lives.

    ``source`` names an entry by a bd id found in exactly one entry's title;
    ``marker`` is the exact opening text of the one marker block in that entry
    to act on. A block runs from its marker line to the next blank line or the
    next marker line. With a ``target`` (an entry key like ``source``) the
    block moves to the end of that entry; without one it is dropped.
    """

    source: str
    marker: str
    reason: str
    target: str | None = None

    @property
    def action(self) -> str:
        return "move" if self.target else "drop"


# The lode-d3snk Update was appended under the lode-fg7w7.3 section by mistake;
# it corrects the lode-yt7q2 entry just before it. Both lode-htkr3 pointers only
# say the d3snk Update is misfiled (one sits in the lode-yt7q2 entry, one in the
# lode-fg7w7.3 section) and are obsolete once it is moved. The third lode-htkr3
# Update (DECIDED, proposals 2 and 3) is the lode-fg7w7.3 section's own.
OVERRIDES: tuple[Override, ...] = (
    Override(
        "lode-fg7w7.3",
        "**Update (lode-d3snk, 2026-11-27):**",
        "answers the lode-yt7q2 entry, per the lode-htkr3 pointer",
        target="lode-yt7q2",
    ),
    Override(
        "lode-yt7q2",
        "**Update (lode-htkr3, 2026-11-27):**",
        "pointer to the misfiled lode-d3snk Update, obsolete once it is moved",
    ),
    Override(
        "lode-fg7w7.3",
        "**Update (lode-htkr3, 2026-11-27):**",
        "pointer saying the lode-d3snk Update above does not belong here, "
        "obsolete once it is moved",
    ),
    # lode-pqhhx Q4=A supersedes lode-tikr's leave-it ruling for the migration
    # only: the no-silent-rewrite guard behind that ruling reads docs/decisions.md,
    # which the cutover removes. By the same ruling the lode-smpk pointer in the
    # lode-sflg section is kept, not dropped like the lode-htkr3 ones, although
    # its "the lode-ucpu Update above" then has no such Update above it.
    Override(
        "lode-sflg",
        "**Update (lode-ucpu, 2026-11-20, maintainer):**",
        "answers the lode-vlok entry, per the lode-smpk pointer",
        target="lode-vlok",
    ),
)


@dataclass
class Entry:
    kind: str  # "bullet" or "section"
    start_line: int  # 1-based line in the source
    title: str
    lines: list[str]
    # (source line, title) of each bullet merged into this section.
    sub_points: list[tuple[int, str]] = field(default_factory=list)
    date: str = ""
    date_source: str = ""
    status: str = "decided"
    status_unclear: str = ""
    status_note: str = ""  # how a title with open wording was read, when not unclear
    entry_id: str = ""
    filename: str = ""


@dataclass
class Applied:
    override: Override
    source_entry: Entry
    target_entry: Entry | None
    text: str
    source_line: int  # 1-based source line of the block's marker


@dataclass
class Result:
    preamble_lines: int
    entries: list[Entry]
    applied: list[Applied] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)
    mapping: list[dict] = field(default_factory=list)
    source_body: str = ""


def _title(kind: str, lines: list[str]) -> str:
    if kind == "section":
        return lines[0][3:].strip()
    joined = "".join(lines[:6])[len("- **") :]
    end = joined.find("**")
    return " ".join((joined[:end] if end != -1 else lines[0][4:]).split())


# Column-zero bullets that follow a ``## `` section but are entries of their own,
# not its sub-points (lode-pqhhx Q1=B). Every other bullet between a section and
# the first bullet listed here is part of the section's entry. A listed bullet
# ends the section, so a bullet after it is an entry of its own, listed or not:
# the second row changes no output and is here so that both are reviewed. Each
# row is a prefix of exactly one bullet title; reviewed like OVERRIDES.
NEW_ENTRY_BULLETS: tuple[tuple[str, str], ...] = (
    (
        "/land SKILL.md does not carry a",
        "its own decision, follows the lode-sflg section without belonging to it",
    ),
    (
        "code-reviewer.md rule inventory amended before the `lode-w18a.9` cut",
        "its own decision (lode-w18a.10), follows the lode-sflg section",
    ),
)


def _listed(entries: list[Entry], prefix: str) -> list[Entry]:
    """The bullet entries a NEW_ENTRY_BULLETS row's ``prefix`` names."""
    return [e for e in entries if e.kind == "bullet" and e.title.startswith(prefix)]


def split_entries(text: str) -> tuple[int, list[Entry]]:
    """``(preamble line count, entries)`` for a decisions.md text.

    A ``## `` section and the column-zero bullets after it form one entry, except
    a bullet named in NEW_ENTRY_BULLETS, which starts an entry of its own. A row
    of that list that does not match exactly one bullet title is an error.
    """
    lines = text.splitlines(keepends=True)
    inside = fence_flags(lines)
    starts = [
        i
        for i, line in enumerate(lines)
        if not inside[i] and _ENTRY_START_RE.match(line)
    ]
    if not starts:
        return len(lines), []
    bounds = [*starts, len(lines)]
    entries: list[Entry] = []
    for lo, hi in pairwise(bounds):
        kind = "section" if lines[lo].startswith("## ") else "bullet"
        title = _title(kind, lines[lo:hi])
        if (
            kind == "bullet"
            and entries
            and entries[-1].kind == "section"
            and not any(title.startswith(p) for p, _reason in NEW_ENTRY_BULLETS)
        ):
            entries[-1].lines.extend(lines[lo:hi])
            entries[-1].sub_points.append((lo + 1, title))
            continue
        entries.append(Entry(kind, lo + 1, title, lines[lo:hi]))
    # A listed bullet is never merged, so the entries hold every match.
    for prefix, _reason in NEW_ENTRY_BULLETS:
        hits = len(_listed(entries, prefix))
        if hits != 1:
            raise ValueError(f"new-entry row {prefix!r} matches {hits} bullet titles")
    return starts[0], entries


def _titled(entries: list[Entry], key: str) -> list[Entry]:
    """The entries whose title carries the bd id ``key`` as a whole id."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(key)}(?![\w-]|\.\w)")
    return [e for e in entries if pattern.search(e.title)]


def find_entry(entries: list[Entry], key: str) -> Entry:
    hits = _titled(entries, key)
    if len(hits) != 1:
        raise ValueError(f"override key {key!r} matches {len(hits)} entry titles")
    return hits[0]


def _find_block(entry: Entry, marker: str) -> tuple[int, int]:
    hits = [
        i
        for i, line in enumerate(entry.lines)
        if line.lstrip().removeprefix("- ").startswith(marker)
    ]
    if len(hits) != 1:
        raise ValueError(
            f"override marker {marker!r} matches {len(hits)} lines in {entry.title!r}"
        )
    lo = hits[0]
    hi = lo + 1
    while (
        hi < len(entry.lines)
        and entry.lines[hi].strip()
        and not _MARKER_LINE_RE.match(entry.lines[hi])
    ):
        hi += 1
    return lo, hi


def apply_overrides(
    entries: list[Entry], overrides: tuple[Override, ...]
) -> list[Applied]:
    """Move or drop each override's block. Every block is located against the
    original lines first, so one override cannot shift another's."""
    located = []
    for ov in overrides:
        source = find_entry(entries, ov.source)
        lo, hi = _find_block(source, ov.marker)
        target = find_entry(entries, ov.target) if ov.target else None
        located.append((ov, source, lo, hi, target))
    applied = [
        Applied(
            ov, source, target, "".join(source.lines[lo:hi]), source.start_line + lo
        )
        for ov, source, lo, hi, target in located
    ]
    removals = sorted(
        ((id(source), lo, hi, source) for _, source, lo, hi, _ in located),
        key=lambda r: (r[0], -r[1]),
    )
    for _, lo, hi, source in removals:
        del source.lines[lo:hi]
    for item in applied:
        if item.target_entry is not None:
            lines = item.target_entry.lines
            at = len(lines)
            while at and not lines[at - 1].strip():
                at -= 1
            lines[at:at] = item.text.splitlines(keepends=True)
    return applied


def _first_paragraph(entry: Entry) -> str:
    out = []
    for line in entry.lines:
        if not line.strip():
            break
        out.append(line)
    return "".join(out)


def derive_date(entry: Entry) -> None:
    for source, text in (
        ("title", entry.title),
        ("first paragraph", _first_paragraph(entry)),
        ("body", "".join(entry.lines)),
    ):
        m = _ISO_DATE_RE.search(text)
        if m:
            entry.date, entry.date_source = m.group(0), source
            return


def fill_missing_dates(entries: list[Entry]) -> None:
    """An entry with no date anywhere takes the preceding entry's date; those
    ahead of the first dated entry take that entry's."""
    prev = next((e.date for e in entries if e.date), None)
    if prev is None:
        raise ValueError("no entry carries a date")
    for entry in entries:
        if entry.date:
            prev = entry.date
        else:
            entry.date, entry.date_source = prev, "inferred from a neighbour"


def _marker_texts(entry: Entry) -> list[str]:
    body = "".join(entry.lines)
    return [body[m.end() : m.end() + 250] for m in _MARKER_RE.finditer(body)]


def derive_status(entry: Entry) -> None:
    if not _OPEN_RE.search(entry.title):
        entry.status = "decided"
        return
    entry.status = "open"
    if _DECIDED_RE.search(entry.title):
        entry.status_unclear = "title has both open and decided wording"
        return
    markers = _marker_texts(entry)
    ruling = next((t for t in markers if _RULING_RE.search(t)), None)
    if ruling is not None:
        entry.status = "decided"
        entry.status_note = f"by Update ({' '.join(ruling.split())[:50]}"
    elif markers:
        entry.status_unclear = "open title; Updates present, none reads as a ruling"
    else:
        entry.status_note = "no Update"


def assign_names(entries: list[Entry], rng: random.Random) -> None:
    taken: set[str] = set()
    for entry in entries:
        while True:
            entry_id = "".join(rng.choice(_ID_CHARS) for _ in range(5))
            if entry_id not in taken:
                break
        taken.add(entry_id)
        slug = re.sub(r"[^a-z0-9]+", "-", entry.title.lower()).strip("-")
        if len(slug) > _SLUG_MAX:
            slug = slug[:_SLUG_MAX].rsplit("-", 1)[0] or slug[:_SLUG_MAX]
        entry.entry_id = entry_id
        entry.filename = f"{entry_id}-{slug or 'entry'}.md"


def render(entry: Entry) -> str:
    return (
        f"---\nid: {entry.entry_id}\ndate: {entry.date}\nstatus: {entry.status}\n"
        f"supersedes: []\nrelated: []\n---\n{''.join(entry.lines)}"
    )


def find_suspects(entries: list[Entry]) -> list[tuple[int, str]]:
    """``(marker line, report row)`` for each marker whose wording or id points
    at an entry other than the one it sits in, and for every column-zero Update
    head (an entry of its own that corrects an earlier one). For the reviewer
    to move or reject."""
    out = []
    for entry in entries:
        if _MARKER_RE.match(entry.lines[0][2:]):
            row = (
                f"line {entry.start_line}: column-zero Update entry "
                f"{entry.title[:70]!r}; a separate entry, not merged into the "
                "entry it corrects"
            )
            out.append((entry.start_line, row))
    for entry in entries:
        body = "".join(entry.lines)
        for m in _MARKER_RE.finditer(body):
            line = entry.start_line + body.count("\n", 0, m.start())
            text = " ".join(body[m.start() : m.start() + 600].split())
            why = []
            if _MISFILED_RE.search(text):
                why.append("wording says it is misfiled")
            elif _POINTS_ELSEWHERE_RE.search(text[:300]):
                why.append("wording points at another entry")
            lead = _MARKER_ID_RE.match(text)
            homes = _titled(entries, lead.group(1)) if lead else []
            if entry not in homes:
                why += [
                    f"its id {lead.group(1)} heads the entry at line {h.start_line}"
                    for h in homes
                ]
            for named in _NAMED_ENTRY_RE.findall(text[:300]):
                homes = _titled(entries, named)
                if homes and entry not in homes:
                    why.append(f"names the {named} entry at line {homes[0].start_line}")
            if why:
                row = (
                    f"line {line} (entry at line {entry.start_line}): "
                    f"{'; '.join(dict.fromkeys(why))}: {text[:70]!r}"
                )
                out.append((line, row))
    return out


def build_mapping(text: str, entries: list[Entry]) -> list[dict]:
    """Every old heading anchor (any level, outside fences) and every entry
    title -> its new file. Anchors repeat as GitHub numbers them (-1, -2)."""
    lines = text.splitlines(keepends=True)
    inside = fence_flags(lines)
    # From the start lines, not ``len(entry.lines)``: an override has changed
    # the length of the entries it touched.
    owner: list[Entry | None] = [None] * len(lines)
    bounds = [*(e.start_line - 1 for e in entries), len(lines)]
    for entry, (lo, hi) in zip(entries, pairwise(bounds), strict=True):
        owner[lo:hi] = [entry] * (hi - lo)
    seen: dict[str, int] = {}
    rows = []
    for i, line in enumerate(lines):
        m = None if inside[i] else _HEADING_RE.match(line)
        if not m:
            continue
        slug = github_slug(m.group(1))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        anchor = slug if n == 0 else f"{slug}-{n}"
        home = owner[i]
        rows.append(
            {
                "kind": "heading",
                "old_anchor": anchor,
                "old_title": m.group(1),
                "old_line": i + 1,
                "new_file": home.filename if home else "README.md",
            }
        )
    # The numbering above is a copy of ``anchor_slugs``'s, which returns no
    # line numbers. It also returns explicit ``<a id>`` anchors, which get no
    # row here, so one of those in the source fails this check too.
    drift = {r["old_anchor"] for r in rows} ^ anchor_slugs(text)
    if drift:
        raise ValueError(f"anchors differ from lode.docs_slug's: {sorted(drift)}")
    for entry in entries:
        bullets = (
            [(entry.start_line, entry.title)]
            if entry.kind == "bullet"
            else entry.sub_points
        )
        rows += [
            {
                "kind": "bullet",
                "old_anchor": None,
                "old_title": title,
                "old_line": line,
                "new_file": entry.filename,
            }
            for line, title in bullets
        ]
    return sorted(rows, key=lambda r: r["old_line"])


def migrate(
    text: str,
    overrides: tuple[Override, ...] | None = None,
    rng: random.Random | None = None,
) -> Result:
    preamble_lines, entries = split_entries(text)
    if not entries:
        raise ValueError("no entries found")
    source_body = "".join(text.splitlines(keepends=True)[preamble_lines:])
    result = Result(preamble_lines, entries, source_body=source_body)
    # Before the overrides, so each suspect's line number is its source line.
    suspects = find_suspects(entries)
    result.applied = apply_overrides(
        entries, OVERRIDES if overrides is None else overrides
    )
    handled = {a.source_line for a in result.applied}
    result.suspects = [row for line, row in suspects if line not in handled]
    for entry in entries:
        derive_date(entry)
        derive_status(entry)
    fill_missing_dates(entries)
    assign_names(entries, rng or random.SystemRandom())
    result.mapping = build_mapping(text, entries)
    return result


def write_entries(target: Path, entries: list[Entry]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        (target / entry.filename).write_text(render(entry), encoding="utf-8")


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    close = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
    return "".join(lines[close + 1 :])


def verify(target: Path, result: Result) -> list[str]:
    """Re-read the written files and check: one file per entry, every Update
    marker accounted for (dropped ones listed), each moved block in its target
    entry's file, and the bodies equal the source minus preamble minus the
    override-affected blocks."""
    problems = []
    names = sorted(p.name for p in target.glob("*.md") if p.name != "README.md")
    if names != sorted(e.filename for e in result.entries):
        return [f"{len(names)} files out for {len(result.entries)} entries in"]
    bodies = {
        e.filename: _strip_frontmatter(
            (target / e.filename).read_text(encoding="utf-8")
        )
        for e in result.entries
    }
    actual = "".join(bodies.values())
    markers_in = len(_MARKER_RE.findall(result.source_body))
    dropped = sum(
        len(_MARKER_RE.findall(a.text))
        for a in result.applied
        if a.target_entry is None
    )
    markers_out = len(_MARKER_RE.findall(actual))
    if markers_in != markers_out + dropped:
        problems.append(
            f"{markers_in} markers in, {markers_out} out, {dropped} dropped"
        )
    expected = result.source_body
    for a in result.applied:
        if expected.count(a.text) != 1:
            problems.append(f"override block {a.override.marker!r} is not unique")
            continue
        expected = expected.replace(a.text, "", 1)
        if a.target_entry is not None:
            if a.text not in bodies[a.target_entry.filename]:
                problems.append(
                    f"moved block {a.override.marker!r} is not in its target entry"
                )
            if actual.count(a.text) != 1:
                problems.append(f"moved block {a.override.marker!r} is not unique")
                continue
            actual = actual.replace(a.text, "", 1)
    if actual != expected:
        problems.append("bodies differ from source beyond the listed overrides")
    return problems


def unmapped_links(repo_root: Path, mapping: list[dict]) -> tuple[int, list[str]]:
    """``(links found, anchors no mapping row covers)`` over the repo's text
    files, for links into ``decisions.md`` headings."""
    known = {r["old_anchor"] for r in mapping if r["old_anchor"]}
    skip = {".git", ".venv", "worktrees", "site", "__pycache__", ".nox", "node_modules"}
    link_re = re.compile(r"decisions\.md#([A-Za-z0-9_%.-]+)")
    found = 0
    missing: list[str] = []
    for directory, subdirs, names in repo_root.walk():
        # Pruned in place, so a skipped tree (a ``.venv`` is about 36,000
        # paths) is never entered.
        subdirs[:] = sorted(d for d in subdirs if d not in skip)
        for name in sorted(names):
            path = directory / name
            try:
                body = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):  # fmt: skip
                continue
            for anchor in link_re.findall(body):
                found += 1
                if anchor not in known:
                    missing.append(f"{path.relative_to(repo_root)}: #{anchor}")
    return found, missing


def render_report(
    result: Result, problems: list[str], links: tuple[int, list[str]] | None
) -> str:
    e = result.entries
    out = [
        f"preamble not migrated: {result.preamble_lines} lines",
        (
            f"entries in: {len(e)} ({sum(x.kind == 'section' for x in e)} sections, "
            f"{sum(x.kind == 'bullet' for x in e)} bullets); files out: {len(e)}"
        ),
        (
            f"Update markers in source body: {len(_MARKER_RE.findall(result.source_body))}; "
            f"out: {sum(len(_MARKER_RE.findall(''.join(x.lines))) for x in e)}"
        ),
        "",
        f"OVERRIDE TABLE ({len(result.applied)}):",
    ]
    for a in result.applied:
        dest = (
            f"moved to entry at line {a.target_entry.start_line}"
            if a.target_entry
            else "dropped"
        )
        out.append(
            f"  {a.override.action} {a.override.marker!r} "
            f"({len(a.text.splitlines())} lines from line {a.source_line}) from "
            f"entry at line {a.source_entry.start_line} ({dest}): {a.override.reason}"
        )
    merged = [(x, line, title) for x in e for line, title in x.sub_points]
    out += [
        "",
        (
            f"SECTION SUB-POINTS ({len(merged)} bullets merged into "
            f"{sum(bool(x.sub_points) for x in e)} sections; one that is an entry "
            "of its own goes in NEW_ENTRY_BULLETS):"
        ),
    ]
    out += [
        f"  line {line} (section at line {x.start_line}): {title[:70]!r}"
        for x, line, title in merged
    ]
    out += ["", f"NEW-ENTRY BULLETS ({len(NEW_ENTRY_BULLETS)}):"]
    out += [
        f"  line {x.start_line}: {x.title[:70]!r}: {reason}"
        for prefix, reason in NEW_ENTRY_BULLETS
        for x in _listed(e, prefix)
    ]
    out += [
        "",
        f"SUSPECTS ({len(result.suspects)}, for the reviewer to add or reject):",
    ]
    out += [f"  {s}" for s in result.suspects]
    worded = [x for x in e if x.status_unclear or x.status_note]
    out += [
        "",
        (
            f"STATUS: {sum(x.status == 'open' for x in e)} open, "
            f"{sum(x.status == 'decided' for x in e)} decided; "
            f"{sum(bool(x.status_unclear) for x in e)} UNCLEAR, defaulted to open. "
            f"Every title with open wording ({len(worded)}):"
        ),
    ]
    out += [
        f"  line {x.start_line}: {x.status} "
        f"({'UNCLEAR: ' + x.status_unclear if x.status_unclear else x.status_note}): "
        f"{x.title[:70]!r}"
        for x in worded
    ]
    guessed = [x for x in e if x.date_source != "title"]
    out += [
        "",
        f"DATES: {len(e) - len(guessed)} from the title; {len(guessed)} guessed:",
    ]
    out += [
        f"  line {x.start_line}: {x.date} ({x.date_source}): {x.title[:60]!r}"
        for x in guessed
    ]
    if links is not None:
        out += [
            "",
            f"LINKS: {links[0]} links into decisions.md headings; {len(links[1])} unmapped",
        ]
        out += [f"  {m}" for m in links[1]]
    out += ["", "VERIFY: " + ("ok" if not problems else "FAILED")]
    out += [f"  {p}" for p in problems]
    return "\n".join(out)


@app.command()
def main(
    source: Annotated[Path, typer.Argument(help="docs/decisions.md to split.")],
    target: Annotated[Path, typer.Argument(help="Output directory; must be empty.")],
    mapping: Annotated[
        Path | None, typer.Option(help="Mapping file; default next to TARGET.")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Seed for repeatable ids.")] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option(help="Also check every link into decisions.md headings here."),
    ] = None,
) -> None:
    """Split decisions.md into per-entry files in TARGET; print the review report."""
    if target.exists() and any(target.iterdir()):
        typer.echo(f"{target} is not empty; refusing to write into it", err=True)
        raise typer.Exit(2)
    result = migrate(
        source.read_text(encoding="utf-8"),
        rng=random.Random(seed) if seed is not None else None,
    )
    write_entries(target, result.entries)
    mapping_path = mapping or target.resolve().parent / f"{target.name}-mapping.json"
    mapping_path.write_text(
        json.dumps(result.mapping, indent=2) + "\n", encoding="utf-8"
    )
    problems = verify(target, result)
    links = unmapped_links(repo_root, result.mapping) if repo_root else None
    typer.echo(render_report(result, problems, links))
    raise typer.Exit(1 if problems or (links and links[1]) else 0)


if __name__ == "__main__":
    app()

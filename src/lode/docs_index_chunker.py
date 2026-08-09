"""Fence-aware structural chunker for the ``docs/`` lookup index (``lode-t6o1.1``).

Splits each file of ``docs/*.md`` into retrievable **units** -- ``(path, line_lo,
line_hi, first_line, body)`` -- for the on-demand SQLite FTS5 index over the docs
corpus (``docs/decisions.md``, entry ``lode-t6o1``; epic ``lode-t6o1``'s Design
field). This module is *only* the chunker; building/querying the FTS5 index is
``lode-t6o1.2``.

**ONE rule, not two** (the decided shape -- a two-rule design was ``/challenge``d
and refuted on measurement; do not re-derive it, see the decision record above):

1. ``boundary`` = the deepest ATX heading level present in the file (h3 if any,
   else h2, else h1).
2. A file with no recurring heading at all (``docs/decisions.md``) splits at
   top-level ``- `` bullets instead.
3. Post-pass: any unit still over :data:`MAX_UNIT_BYTES` is hard-split at the
   next boundary down -- heading-split units re-split by bullet, bullet-split
   units (already the deepest rule) re-split by blank-line-delimited paragraph.
   A unit that is still oversized after a paragraph split (nothing smaller to
   split by) is emitted as-is; this has not occurred against the real corpus,
   which is why the invariant is asserted by a test rather than merely hoped
   for.

**Fence-aware.** A ``## `` or ``- `` line inside a fenced code block (opened by
a line starting with ` ``` ` or ``~~~``) is never treated as a boundary -- it is
sample markdown/shell inside someone's example, not real document structure.
The measurement pass that produced the epic's size numbers used fence tracking;
a non-fence-aware chunker produces different (wrong) units.

Line numbers are 1-based and inclusive on both ends, and are exact: for a unit
``u``, ``"\\n".join(path.read_text().splitlines()[u.line_lo - 1 : u.line_hi])
== u.body`` -- a caller can ``Read`` exactly that range.

Each file is also tagged :func:`classify`-ied as ``"decision-record"`` or
``"reference/process"`` (consumed by the ``--class`` filter in the CLI ticket,
``lode-t6o1.2``) -- per the decision record's explicit split, not a guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The hard size invariant (docs/decisions.md, lode-t6o1): no unit may exceed
#: this many UTF-8 encoded bytes. Asserted by a test over the real corpus.
MAX_UNIT_BYTES = 16384

_FENCE_RE = re.compile(r"^(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s")
_BULLET_RE = re.compile(r"^-\s")

#: The decided reference/process set (docs/decisions.md, "Left open,
#: deliberately") -- the complement is "decision-record". Named explicitly in
#: the decision record, not derived from any other property of the files.
_REFERENCE_PROCESS_STEMS = frozenset(
    {"keybindings", "release", "onboarding", "tui", "editing", "test-suite-audit"}
)


@dataclass(frozen=True)
class Unit:
    """One retrievable chunk of a docs/ file."""

    path: str
    line_lo: int
    line_hi: int
    first_line: str
    body: str
    doc_class: str


def classify(path: Path | str) -> str:
    """Tag a docs/ file as ``"decision-record"`` or ``"reference/process"``."""
    return (
        "reference/process"
        if Path(path).stem in _REFERENCE_PROCESS_STEMS
        else "decision-record"
    )


def _fence_flags(lines: list[str]) -> list[bool]:
    """Per-line: is this line's content inside a fenced code block?

    The fence-delimiter line itself (the ``` opener/closer) is reported as
    *not* inside the fence -- it can't be a heading/bullet anyway, so this
    only matters for readability of the flag list.
    """
    flags = []
    in_fence = False
    for line in lines:
        flags.append(in_fence)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
    return flags


def _deepest_heading_level(lines: list[str], fence_flags: list[bool]) -> int:
    """The deepest ATX heading level (1-6) present outside fences; 0 if none."""
    deepest = 0
    for line, in_fence in zip(lines, fence_flags, strict=True):
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            deepest = max(deepest, len(m.group(1)))
    return deepest


def _split_range(
    lines: list[str], line_lo: int, line_hi: int, is_boundary: Callable[[int], bool]
) -> list[tuple[int, int]]:
    """Split the 1-based inclusive range ``[line_lo, line_hi]`` at every 0-based
    line index for which ``is_boundary(i)`` holds.

    Content before the first boundary (if any) becomes its own leading unit
    (e.g. a file's text before its first heading). If no boundary is found in
    the range at all, the whole range is returned unchanged as a single unit.
    """
    lo0, hi0 = line_lo - 1, line_hi - 1
    boundaries = [i for i in range(lo0, hi0 + 1) if is_boundary(i)]
    if not boundaries:
        return [(line_lo, line_hi)]
    ranges: list[tuple[int, int]] = []
    if boundaries[0] != lo0:
        ranges.append((line_lo, boundaries[0]))  # preamble before the first boundary
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] - 1 if i + 1 < len(boundaries) else hi0
        ranges.append((start + 1, end + 1))
    return ranges


def _unit_bytes(lines: list[str], line_lo: int, line_hi: int) -> int:
    return len("\n".join(lines[line_lo - 1 : line_hi]).encode("utf-8"))


def _hard_split(
    lines: list[str],
    fence_flags: list[bool],
    line_lo: int,
    line_hi: int,
    *,
    try_bullet: bool,
) -> list[tuple[int, int]]:
    """Post-pass: recursively hard-split an oversized unit at the next boundary
    down. ``try_bullet`` is True only for a unit that came from the heading
    split (a bullet-split or paragraph-split unit is already at (or past) the
    deepest rule, so it goes straight to paragraph splitting)."""
    if _unit_bytes(lines, line_lo, line_hi) <= MAX_UNIT_BYTES:
        return [(line_lo, line_hi)]

    if try_bullet:

        def bullet_boundary(i: int) -> bool:
            return not fence_flags[i] and bool(_BULLET_RE.match(lines[i]))

        sub_ranges = _split_range(lines, line_lo, line_hi, bullet_boundary)
        if len(sub_ranges) > 1:
            out: list[tuple[int, int]] = []
            for slo, shi in sub_ranges:
                out.extend(_hard_split(lines, fence_flags, slo, shi, try_bullet=False))
            return out
        # No bullets in range: fall through to paragraph splitting below.

    def paragraph_boundary(i: int) -> bool:
        if fence_flags[i] or lines[i].strip() == "":
            return False
        if i == 0:
            return True
        prev_blank = lines[i - 1].strip() == "" and not fence_flags[i - 1]
        return prev_blank

    sub_ranges = _split_range(lines, line_lo, line_hi, paragraph_boundary)
    if len(sub_ranges) > 1:
        out = []
        for slo, shi in sub_ranges:
            # Nothing smaller than a paragraph to split by; recursing here
            # only re-checks size (try_bullet=False keeps it in the paragraph
            # branch) and terminates, since a range with no further paragraph
            # boundary returns itself unchanged from _split_range above.
            out.extend(_hard_split(lines, fence_flags, slo, shi, try_bullet=False))
        return out
    # No paragraph boundary either (a single oversized paragraph/line): emit
    # as-is. Has not been observed against the real corpus.
    return [(line_lo, line_hi)]


def chunk_file(path: Path | str, text: str) -> list[Unit]:
    """Chunk one markdown file's text into :class:`Unit` objects."""
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return []
    fence_flags = _fence_flags(lines)
    deepest = _deepest_heading_level(lines, fence_flags)

    if deepest:

        def primary_boundary(i: int) -> bool:
            if fence_flags[i]:
                return False
            m = _HEADING_RE.match(lines[i])
            return bool(m) and len(m.group(1)) == deepest
    else:

        def primary_boundary(i: int) -> bool:
            return not fence_flags[i] and bool(_BULLET_RE.match(lines[i]))

    ranges = _split_range(lines, 1, n, primary_boundary)

    final_ranges: list[tuple[int, int]] = []
    for lo, hi in ranges:
        final_ranges.extend(
            _hard_split(lines, fence_flags, lo, hi, try_bullet=bool(deepest))
        )

    doc_class = classify(path)
    units = []
    for lo, hi in final_ranges:
        body_lines = lines[lo - 1 : hi]
        body = "\n".join(body_lines)
        units.append(
            Unit(
                path=str(path),
                line_lo=lo,
                line_hi=hi,
                first_line=body_lines[0] if body_lines else "",
                body=body,
                doc_class=doc_class,
            )
        )
    return units


def chunk_corpus(docs_dir: Path | str) -> list[Unit]:
    """Chunk every ``*.md`` file directly under ``docs_dir``, sorted by name."""
    docs_dir = Path(docs_dir)
    units: list[Unit] = []
    for md_path in sorted(docs_dir.glob("*.md")):
        units.extend(chunk_file(md_path, md_path.read_text(encoding="utf-8")))
    return units

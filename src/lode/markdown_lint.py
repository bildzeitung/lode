"""Pure markdown lint rules for the note editor's inline squiggles (``lode-o7pf.2``).

Five hand-rolled rules over ``(lines, index, in_fence, cursor)`` returning
CHARACTER ranges ``[start, end)`` on ``lines[index]`` (never byte offsets).
The rule table and its rulings live in ``docs/editing.md``; no Textual import,
so the module is testable without a UI.

Fence context is the caller's ``fence_flags`` list. ``fence_flags`` reports an
opener line as outside the fence, so openers are recognised here with
``match_fence_marker``; fence content and delimiter lines yield nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from lode.fence_parsing import match_fence_marker

Range = tuple[int, int]

_TRAILING_WS_RE = re.compile(r"[ \t]+$")
_HEADING_RE = re.compile(r" {0,3}(#{1,6})(?=[ \t]|$)")
_EXTRA_SPACES_RE = re.compile(r"( {2,})(?=\S)")
_EMPTY_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*\)")
_BACKTICK_RUN_RE = re.compile(r"`+")


def _code_spans(line: str) -> list[Range]:
    """Inline code spans: a backtick run closed by a same-length run on this
    line. A run with no matching closer is literal text."""
    runs = [(m.start(), m.end()) for m in _BACKTICK_RUN_RE.finditer(line)]
    spans: list[Range] = []
    i = 0
    while i < len(runs):
        length = runs[i][1] - runs[i][0]
        for j in range(i + 1, len(runs)):
            if runs[j][1] - runs[j][0] == length:
                spans.append((runs[i][0], runs[j][1]))
                i = j
                break
        i += 1
    return spans


def _outside_spans(ranges: list[Range], spans: list[Range]) -> list[Range]:
    return [r for r in ranges if not any(r[0] < s_end and s_start < r[1] for s_start, s_end in spans)]


def _is_blank(line: str) -> bool:
    return not line.strip()


def lint_line_tagged(
    lines: Sequence[str],
    index: int,
    in_fence: Sequence[bool],
    cursor: tuple[int, int] | None = None,
) -> list[tuple[str, int, int]]:
    """``(rule_id, start, end)`` for every violation on ``lines[index]``,
    sorted by position. ``cursor`` is the caret ``(row, column)``, read only by
    MD009."""
    line = lines[index]
    if in_fence[index] or match_fence_marker(line.strip()) is not None:
        return []
    found: list[tuple[str, int, int]] = []
    spans = _code_spans(line)

    m = _TRAILING_WS_RE.search(line)
    if m and m.group() != "  " and cursor != (index, m.end()):
        found.append(("MD009", m.start(), m.end()))

    for start, end in _outside_spans([(i, i + 1) for i, c in enumerate(line) if c == "\t"], spans):
        found.append(("MD010", start, end))

    h = _HEADING_RE.match(line)
    if h:
        extra = _EXTRA_SPACES_RE.match(line, h.end())
        if extra:
            found.append(("MD019", extra.start() + 1, extra.end()))
        before_ok = index == 0 or _is_blank(lines[index - 1])
        after_ok = index == len(lines) - 1 or _is_blank(lines[index + 1])
        if not (before_ok and after_ok):
            text_start = len(line) - len(line[h.end() :].lstrip())
            text_end = len(line.rstrip())
            if text_start >= text_end:
                found.append(("MD022", h.start(1), h.end(1)))
            else:
                found.append(("MD022", text_start, text_end))

    for start, end in _outside_spans([(m.start(), m.end()) for m in _EMPTY_LINK_RE.finditer(line)], spans):
        found.append(("MD042", start, end))

    return sorted(found, key=lambda f: (f[1], f[2]))


def lint_line(
    lines: Sequence[str],
    index: int,
    in_fence: Sequence[bool],
    cursor: tuple[int, int] | None = None,
) -> list[Range]:
    """Character ranges ``[start, end)`` to underline on ``lines[index]``."""
    return [(s, e) for _, s, e in lint_line_tagged(lines, index, in_fence, cursor)]

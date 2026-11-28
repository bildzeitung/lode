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

_HEADING_RE = re.compile(r" {0,3}(#{1,6})(?=[ \t]|$)")
_EXTRA_SPACES_RE = re.compile(r" {2,}(?=\S)")
#: Trailing whitespace plus an optional ATX closing sequence (``# title ##``),
#: which CommonMark keeps out of the heading's text.
_HEADING_TAIL_RE = re.compile(r"(?:[ \t]+#+)?[ \t]*$")
#: The leading ``(?<!!)``: ``![alt]()`` is an image, which MD042 does not
#: cover. The text may hold one level of nested brackets (``[a[b]c]()``,
#: ``[![alt](src)]()``) but not a nested link (a pair followed by ``(`` and not
#: led by ``!``): CommonMark lets no link hold another, so the inner one is the
#: link. The two nested alternatives exclude each other on the ``!``, and
#: neither starts on a character the first accepts, so the repeat cannot
#: backtrack badly.
_EMPTY_LINK_RE = re.compile(
    r"(?<!!)\[(?:[^\[\]]|(?<!!)\[[^\[\]]*\](?!\()|(?<=!)\[[^\[\]]*\])*\]\(\s*\)"
)
#: What ``_mask_inline`` rewrites or steps over, tried in this order at each
#: position: a backslash escape, a code span, an unclosed backtick run. The
#: escape comes first so an escaped backtick opens nothing; a backslash inside
#: a span is never reached, because the span is consumed whole. An unclosed run
#: is consumed whole too, so its tail cannot open a shorter span. The span
#: alternative is ``lode.docs_slug._INLINE_CODE_RE`` without its leading
#: lookbehind, which would stop a span opening right after an escaped backtick.
_MASK_TOKEN_RE = re.compile(r"\\(?P<esc>.)|(?P<run>`+)(?!`).+?(?<!`)(?P=run)(?!`)|`+")


def _mask_token(m: re.Match[str]) -> str:
    if m["run"]:
        return "`" * len(m.group())
    if m["esc"] in ("[", "]", "!"):
        return "__"
    return m.group()


def _mask_inline(line: str) -> str:
    """``line`` with every inline code span (a backtick run closed by a
    same-length run on this line) overwritten by backticks, and every escaped
    bracket or exclamation mark (``\\[``, ``\\]``, ``\\!``) by two underscores,
    so offsets hold and no rule can match inside a span, on a literal bracket,
    or read a literal ``!`` as an image marker. A span in a link's text leaves
    the link matchable: ``[`f()`]()`` is still an empty link."""
    # Most lines that reach here hold a link and neither character.
    if "`" not in line and "\\" not in line:
        return line
    return _MASK_TOKEN_RE.sub(_mask_token, line)


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

    # str.rstrip, not a ``[ \t]+$`` search: the regex retries at every
    # interior space, quadratic in a long mid-line whitespace run.
    run_start = len(line.rstrip(" \t"))
    if line[run_start:] not in ("", "  ") and cursor != (index, len(line)):
        found.append(("MD009", run_start, len(line)))

    # Both inline rules need one of these; most lines have neither, and this
    # runs for every line a repaint draws.
    if "\t" in line or "](" in line:
        masked = _mask_inline(line)
        if "\t" in masked:
            found += [("MD010", i, i + 1) for i, c in enumerate(masked) if c == "\t"]
        found += [("MD042", *m.span()) for m in _EMPTY_LINK_RE.finditer(masked)]

    h = _HEADING_RE.match(line)
    if h:
        extra = _EXTRA_SPACES_RE.match(line, h.end())
        if extra:
            found.append(("MD019", extra.start() + 1, extra.end()))
        before_ok = index == 0 or _is_blank(lines[index - 1])
        after_ok = index == len(lines) - 1 or _is_blank(lines[index + 1])
        if not (before_ok and after_ok):
            text_start = len(line) - len(line[h.end() :].lstrip())
            text_end = _HEADING_TAIL_RE.search(line, h.end()).start()
            # The bounds cross when the heading is only a closing sequence
            # (``## ##``): the tail swallows the space the text would start after.
            if text_start >= text_end:
                found.append(("MD022", h.start(1), h.end(1)))
            else:
                found.append(("MD022", text_start, text_end))

    return sorted(found, key=lambda f: (f[1], f[2]))


def lint_line(
    lines: Sequence[str],
    index: int,
    in_fence: Sequence[bool],
    cursor: tuple[int, int] | None = None,
) -> list[Range]:
    """Character ranges ``[start, end)`` to underline on ``lines[index]``."""
    return [(s, e) for _, s, e in lint_line_tagged(lines, index, in_fence, cursor)]

"""ONE importable CommonMark fence-parsing primitive (``lode-ee7b``).

Hosts the three CommonMark rules every fence-aware scanner in this repo needs
to agree on: a fence may be indented, is opened by a run of three-or-more
backticks or tildes, and is closed only by a run of the SAME marker character
that is AT LEAST AS LONG as the opener (so a ```` ``` ```` line inside a
four-backtick block, or a ``~~~`` line inside a ```` ``` ````-opened block, is
content, not a close).

Importable from **both** ``src/`` and ``tests/`` -- this module is under
``src/lode/`` specifically so production code (``docs_index_chunker.py``) can
use it without tests/ needing to import from src/ or vice versa creating a
one-way dependency tests/ already has on src/ anyway (pytest's own
``sys.path`` setup makes ``src/`` importable from ``tests/``).

Before this module, the same three rules were independently hand-rolled
THREE times: ``tests/conftest.py``'s ``fence_scan`` (test-side gates),
``src/lode/docs_index_chunker.py``'s former ``_fence_flags`` (the docs-index
chunker), and none in ``scripts/check_links.py`` -- ``check_links.py``'s own
``_content_lines`` deliberately implements a DIFFERENT, simpler rule (toggles
on ANY fence-looking line, regardless of marker character or length) and is
NOT a consumer of this module; see its own docstring for why.

``tests/conftest.py``'s ``fence_scan`` is a consumer of the two primitives
below (:func:`match_fence_marker`, :func:`closes_fence`) rather than a
re-implementation, though it stays the sole home of everything ELSE it adds on
top: blockquote-marker stripping, info-string capture, block-ordinal
numbering, and unterminated-fence flushing. Those are consumer-side features,
not part of the shared CommonMark rule set itself.
"""

from __future__ import annotations

import re

#: A fence marker: three-or-more backticks, or three-or-more tildes, plus
#: whatever info string follows. Matched against a line's STRIPPED content
#: (leading whitespace already removed by the caller), since a fence may be
#: indented -- e.g. nested under a markdown list item.
_FENCE_MARKER_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")

#: A fence delimiter matched directly against a line's LEADING whitespace --
#: for a caller that only needs a per-line "is this a fence delimiter at all"
#: boolean flag and does not otherwise strip the line first (docs_index_chunker's
#: use). Equivalent to stripping and running ``_FENCE_MARKER_RE`` at the start
#: of the (possibly indented) run.
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def match_fence_marker(stripped: str) -> tuple[str, str] | None:
    """``(marker, info)`` if ``stripped`` (already whitespace-stripped) opens
    or closes a fence, else ``None``. ``marker`` is the literal run of
    backticks/tildes (e.g. ``"```"``, ``"````"``, ``"~~~"``); ``info`` is
    everything after it verbatim (an opener's info string, or a closer's
    trailing garbage, which callers ignore)."""
    m = _FENCE_MARKER_RE.match(stripped)
    if not m:
        return None
    return m.group(1), m.group(2)


def closes_fence(stripped: str, fence: str) -> bool:
    """Whether ``stripped`` (already whitespace-stripped) closes an open
    ``fence`` -- CommonMark's closing rule: a run of the SAME marker
    character, at least as long as the opener, and nothing else on the line."""
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def fence_flags(lines: list[str]) -> list[bool]:
    """Per-line: is this line's content inside a fenced code block?

    The opening delimiter line is reported as *not* inside the fence; the
    closing one *is*. A leading indent is allowed: fences nested in a list
    item are real fences. No blockquote handling -- a caller that needs
    blockquote-marker stripping first (``tests/conftest.py``'s ``fence_scan``)
    does that itself before consulting :func:`match_fence_marker` /
    :func:`closes_fence` directly, since the strip changes what "stripped"
    means per line.
    """
    flags = []
    open_marker: str | None = None
    for line in lines:
        flags.append(open_marker is not None)
        m = _FENCE_LINE_RE.match(line)
        if not m:
            continue
        marker = m.group(1)
        if open_marker is None:
            open_marker = marker
        elif marker[0] == open_marker[0] and len(marker) >= len(open_marker):
            open_marker = None
    return flags

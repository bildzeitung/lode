"""``TextArea`` that underlines markdown lint violations (``lode-o7pf.3``).

``get_line`` is Textual's documented hook for stylizing a line's ``Text``;
the ranges come from :mod:`lode.markdown_lint`. Fence context is one scan per
render pass: the first ``get_line`` of a pass fills the memo and
``render_lines`` drops it when the pass ends, so a cached-line refresh (cursor
blink) scans nothing. The lint ranges of a line are memoized per pass the same
way, because Textual fetches a soft-wrapped line once per wrapped row. No
change handler or invalidation is needed because edits, undo and redo already
clear Textual's line cache and force a new render pass.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import TextArea

from lode.fence_parsing import fence_flags
from lode.markdown_lint import Range, lint_line

_UNDERLINE = Style(underline=True)


class LintTextArea(TextArea):
    """A ``TextArea`` whose lines carry lint underlines."""

    #: ``fence_flags`` for the render pass in progress, ``None`` until that
    #: pass first needs it and always ``None`` outside a pass.
    lint_fence_flags: list[bool] | None = None
    #: Lint ranges by line index: a dict while a render pass is in progress,
    #: ``None`` outside one. Only a pass may keep either memo. Textual also
    #: fetches a line outside one (mouse hit-testing calls ``render_line``
    #: directly), possibly after an edit and before the repaint, and stores
    #: what it gets in its line cache: a memo kept from an earlier pass would
    #: put a wrong underline on screen until the next edit.
    lint_ranges: dict[int, list[Range]] | None = None

    def render_lines(self, crop: Region) -> list[Strip]:
        self.lint_ranges = {}
        try:
            return super().render_lines(crop)
        finally:
            self.lint_fence_flags = None
            self.lint_ranges = None

    def get_line(self, line_index: int) -> Text:
        text = super().get_line(line_index)
        memo = self.lint_ranges
        ranges = None if memo is None else memo.get(line_index)
        if ranges is None:
            lines = self.document.lines
            flags = self.lint_fence_flags
            if flags is None:
                flags = fence_flags(lines)
                if memo is not None:
                    self.lint_fence_flags = flags
            ranges = lint_line(lines, line_index, flags, self.cursor_location)
            if memo is not None:
                memo[line_index] = ranges
        for start, end in ranges:
            text.stylize(_UNDERLINE, start, end)
        return text

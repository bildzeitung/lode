"""``TextArea`` that underlines markdown lint violations (``lode-o7pf.3``).

``get_line`` is Textual's documented hook for stylizing a line's ``Text``;
the ranges come from :mod:`lode.markdown_lint`. Fence context is one scan per
render pass: ``render_lines`` drops the memo and the first ``get_line`` of the
pass refills it, so a cached-line refresh (cursor blink) scans nothing. No
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
from lode.markdown_lint import lint_line

_UNDERLINE = Style(underline=True)


class LintTextArea(TextArea):
    """A ``TextArea`` whose lines carry lint underlines."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lint_fence_flags: list[bool] | None = None

    def render_lines(self, crop: Region) -> list[Strip]:
        self.lint_fence_flags = None
        return super().render_lines(crop)

    def get_line(self, line_index: int) -> Text:
        text = super().get_line(line_index)
        lines = self.document.lines
        if self.lint_fence_flags is None or len(self.lint_fence_flags) != len(lines):
            self.lint_fence_flags = fence_flags(lines)
        for start, end in lint_line(
            lines, line_index, self.lint_fence_flags, self.cursor_location
        ):
            text.stylize(_UNDERLINE, start, end)
        return text

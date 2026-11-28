"""LintTextArea: lint underlines via get_line (lode-o7pf.3)."""

from __future__ import annotations

import ast
import asyncio
import functools
import time
from pathlib import Path

import pytest
from rich.style import Style
from textual.app import App, ComposeResult
from textual.widgets import TextArea

from lode.fence_parsing import fence_flags
from lode.markdown_lint import lint_line
from lode.tui.screens._markdown_area import NOTE_BODY_THEME
from lode.tui.widgets import lint_text_area
from lode.tui.widgets.lint_text_area import LintTextArea


def _sync(fn):
    @functools.wraps(fn)
    def run() -> None:
        asyncio.run(fn())

    return run


class _App(App):
    def __init__(self, **kw) -> None:
        super().__init__()
        self.kw = kw

    def compose(self) -> ComposeResult:
        yield LintTextArea(id="ta", **self.kw)


def _underlined(area: LintTextArea, i: int) -> list[tuple[int, int]]:
    return [
        (s.start, s.end)
        for s in area.get_line(i).spans
        if getattr(s.style, "underline", False)
    ]


def _expected(area: LintTextArea, i: int) -> list[tuple[int, int]]:
    lines = area.document.lines
    return lint_line(lines, i, fence_flags(lines), area.cursor_location)


TEXT = "# Title\nbody\t\n\n```\nx\t\n```\n[a]()\ntrail  x  \n"


@_sync
async def test_spans_match_rule_engine() -> None:
    app = _App()
    async with app.run_test() as pilot:
        area = app.query_one(LintTextArea)
        area.load_text(TEXT)
        await pilot.pause()
        flagged = 0
        for i in range(len(area.document.lines)):
            assert _underlined(area, i) == _expected(area, i)
            flagged += bool(_expected(area, i))
        assert flagged >= 2
        assert _underlined(area, 0) == [(2, 7)]
        assert _underlined(area, 4) == []  # the tab inside the fence
        assert _underlined(area, 6) == [(0, 5)]


@_sync
async def test_edit_undo_redo_not_stale() -> None:
    app = _App()
    async with app.run_test() as pilot:
        area = app.query_one(LintTextArea)
        area.load_text("ok\n")
        await pilot.pause()
        assert _underlined(area, 0) == []
        area.cursor_location = (1, 0)
        area.insert("[a]()", location=(0, 0))
        await pilot.pause()
        assert _underlined(area, 0) == _expected(area, 0) != []
        area.undo()
        await pilot.pause()
        assert _underlined(area, 0) == []
        area.redo()
        await pilot.pause()
        assert _underlined(area, 0) == _expected(area, 0) != []


@_sync
async def test_fence_toggle_between_render_passes() -> None:
    # Textual's mouse hit-testing calls render_line outside a render pass and
    # caches the strip, so get_line must be right there too: after an edit
    # that opens or closes a fence without changing the line count, before
    # any repaint.
    app = _App()
    async with app.run_test() as pilot:
        area = app.query_one(LintTextArea)
        area.load_text("a\t\nb\t\nc\t\n")
        area.cursor_location = (3, 0)
        await pilot.pause()
        area.insert("x", location=(2, 0))
        await pilot.pause()
        assert _underlined(area, 1) != []
        area.replace("```", (0, 0), (0, 2))
        assert _underlined(area, 1) == []
        area.replace("a\t", (0, 0), (0, 3))
        assert _underlined(area, 1) == _expected(area, 1) != []
        area.replace("```", (0, 0), (0, 2))
        area.render_line(1)
        await pilot.pause()
        assert not any(s.style and s.style.underline for s in area.render_line(1))


@_sync
async def test_soft_wrap_long_line() -> None:
    app = _App(soft_wrap=True)
    async with app.run_test(size=(40, 20)) as pilot:
        area = app.query_one(LintTextArea)
        area.load_text("word " * 60 + "[a]() tail\t\n")
        area.cursor_location = (1, 0)
        await pilot.pause()
        got = _underlined(area, 0)
        assert got == _expected(area, 0) and got


@_sync
async def test_cursor_line_trailing_space() -> None:
    app = _App()
    async with app.run_test() as pilot:
        area = app.query_one(LintTextArea)
        area.load_text("abc\nnext")
        area.focus()
        area.cursor_location = (0, 3)
        await pilot.press("space")
        await pilot.pause()
        assert area.cursor_location == (0, 4)
        assert _underlined(area, 0) == []
        area.cursor_location = (1, 0)
        await pilot.pause()
        assert _underlined(area, 0) == [(3, 4)]


@_sync
async def test_theme_syntax_colours_unchanged() -> None:
    # Line 0 is a flagged heading and line 1 holds a flagged tab, so the
    # underline has to coexist with a syntax colour and with tab expansion.
    sample = "# Head\nbody\tx\n\n- item\n\n```\ncode\n```\n"
    no_underline = Style(underline=False)

    async def cells(cls: type[TextArea]) -> list[list[tuple[str, Style, bool]]]:
        class A(App):
            def compose(self) -> ComposeResult:
                yield cls(sample, language="markdown", id="ta")

        app = A()
        async with app.run_test(size=(40, 12)) as pilot:
            area = app.query_one(TextArea)
            area.register_theme(NOTE_BODY_THEME)
            area.theme = NOTE_BODY_THEME.name
            area.cursor_location = (8, 0)
            await pilot.pause()
            return [
                [
                    (ch, (s.style or Style()) + no_underline, bool(s.style.underline))
                    for s in area.render_line(y)
                    for ch in s.text
                ]
                for y in range(9)
            ]

    lint, plain = await cells(LintTextArea), await cells(TextArea)
    assert [[c[:2] for c in row] for row in lint] == [
        [c[:2] for c in row] for row in plain
    ]
    assert not any(c[2] for row in plain for c in row)
    assert "".join(c[0] for c in lint[0] if c[2]) == "Head"
    assert any(c[2] for c in lint[1])
    assert not any(c[2] for row in lint[2:] for c in row)


def test_no_private_attribute_access() -> None:
    tree = ast.parse(Path(lint_text_area.__file__).read_text())
    bad = [
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and n.attr.startswith("_")
        and not (n.attr.startswith("__") and n.attr.endswith("__"))
    ]
    assert bad == []


@pytest.mark.serial
@_sync
async def test_perf_2000_lines() -> None:
    block = ["# H", "", "text  ", "```", "code\t", "```", "[a]()", ""]
    text = "\n".join(block * 250)
    app = _App()
    async with app.run_test() as pilot:
        area = app.query_one(LintTextArea)
        area.load_text(text)
        await pilot.pause()
        n = len(area.document.lines)
        assert n >= 2000
        # What render_lines sets around a pass: without it every get_line
        # rescans the fences, which is not what a redraw costs.
        area.lint_in_render_pass = True
        best = float("inf")
        for _ in range(5):
            area.lint_fence_flags = None
            t0 = time.perf_counter()
            for i in range(n):
                area.get_line(i)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.05

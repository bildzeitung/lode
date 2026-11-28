"""Unit tests for lode.markdown_lint (lode-o7pf.2)."""

import subprocess
import sys

from lode.fence_parsing import fence_flags
from lode.markdown_lint import lint_line, lint_line_tagged


def _lint(lines, index, cursor=None):
    return lint_line_tagged(lines, index, fence_flags(lines), cursor)


def test_md009_trailing_whitespace_exact_range_non_ascii():
    assert _lint(["café 🙂  x   "], 0) == [("MD009", 9, 12)]


def test_md009_two_trailing_spaces_exempt_one_and_three_flagged():
    assert _lint(["a  "], 0) == []
    assert _lint(["a "], 0) == [("MD009", 1, 2)]
    assert _lint(["a   "], 0) == [("MD009", 1, 4)]


def test_md009_cursor_at_run_end_not_flagged_elsewhere_flagged():
    lines = ["é "]
    assert _lint(lines, 0, cursor=(0, 2)) == []
    assert _lint(lines, 0, cursor=(0, 1)) == [("MD009", 1, 2)]
    assert _lint(lines, 0, cursor=(1, 2)) == [("MD009", 1, 2)]


def test_md010_each_tab_non_ascii():
    assert _lint(["é\ta\tb"], 0) == [("MD010", 1, 2), ("MD010", 3, 4)]


def test_md019_extra_spaces_after_marker_non_ascii():
    assert _lint(["", "##   é title", ""], 1) == [("MD019", 3, 5)]


def test_md018_dropped():
    assert _lint(["#1234 x"], 0) == []
    assert _lint(["#Heading"], 0) == []


def test_md022_missing_blank_underlines_heading_text_non_ascii():
    lines = ["intro", "# café ☕", "body"]
    assert _lint(lines, 1) == [("MD022", 2, 8)]


def test_md022_closing_sequence_is_not_heading_text():
    assert _lint(["p", "# title ##  ", ""], 1) == [("MD022", 2, 7)]
    assert _lint(["p", "## ##", ""], 1) == [("MD022", 0, 2)]
    assert _lint(["p", "# a#", ""], 1) == [("MD022", 2, 4)]


def test_md022_buffer_edges_exempt():
    assert _lint(["# t", ""], 0) == []
    assert _lint(["", "# t"], 1) == []
    assert _lint(["# t"], 0) == []


def test_md022_bare_heading_flags_marker_run_and_whitespace_neighbour_is_blank():
    assert _lint(["x", "##", ""], 1) == [("MD022", 0, 2)]
    assert _lint(["   ", "## t", "\t"], 1) == []


def test_md042_empty_link_whole_range_non_ascii():
    assert _lint(["é [text]() z"], 0) == [("MD042", 2, 10)]


def test_md042_skips_images():
    assert _lint(["![alt]() [x]()"], 0) == [("MD042", 9, 14)]


def test_md042_code_span_in_link_text_is_still_a_link():
    assert _lint(["see [`f()`]() x"], 0) == [("MD042", 4, 13)]
    assert _lint(["`[x`]() b"], 0) == []
    assert _lint(["[a `b]() c`"], 0) == []
    assert _lint(["`[` [x]()"], 0) == [("MD042", 4, 9)]


def test_inline_code_span_exempts_tab_and_empty_link():
    assert _lint(["`a\tb [x]()`"], 0) == []
    assert _lint(["``a\tb`` [x]()"], 0) == [("MD042", 8, 13)]
    assert _lint(["a\tb [x]()"], 0) == [("MD010", 1, 2), ("MD042", 4, 9)]
    assert _lint(["`unclosed\tx"], 0) == [("MD010", 9, 10)]


def test_fenced_content_and_delimiters_yield_nothing():
    lines = ["~~~", "a\t ", "#  h", "~~~", "- item", "  ```py ", "  x\t", "  ```"]
    assert all(_lint(lines, i) == [] for i in range(len(lines)))
    assert _lint([*lines, "x\ty"], len(lines)) == [("MD010", 1, 2)]


def test_lint_line_returns_plain_ranges():
    assert lint_line(["a "], 0, [False]) == [(1, 2)]


def test_module_imports_nothing_from_textual():
    code = "import sys, lode.markdown_lint; sys.exit(any(m.startswith('textual') for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_escaped_backtick_opens_no_code_span():
    # Backticks at 1 and 7 are literal, so the tab and empty link are outside any span.
    assert _lint(["\\`\tx\\` [b]()"], 0) == [("MD010", 2, 3), ("MD042", 7, 12)]


def test_escaped_backtick_does_not_hide_link_in_real_span():
    # CommonMark: the first run is escaped, the later pair is the span.
    assert _lint(["\\`a `[x]()` b"], 0) == []


def test_backslash_inside_code_span_is_literal():
    # The span is `\`; the empty link after it is outside.
    assert _lint(["`\\` [x]()"], 0) == [("MD042", 5, 9)]


def test_link_text_with_nested_brackets_is_flagged():
    assert _lint(["[a[b]c]()"], 0) == [("MD042", 0, 9)]
    assert _lint(["[a[b]()"], 0) == [("MD042", 2, 7)]


def test_escaped_open_bracket_is_not_a_link():
    assert _lint(["\\[x]()"], 0) == []
    assert _lint(["[a\\]b]()"], 0) == [("MD042", 0, 8)]

"""Direct unit tests for the ONE importable fence parser (lode-ee7b,
src/lode/fence_parsing.py) -- the primitives ``docs_index_chunker.py`` and
``tests/conftest.py``'s ``fence_scan`` both now build on."""

from __future__ import annotations

from lode.fence_parsing import closes_fence, fence_flags, match_fence_marker


def test_match_fence_marker_backtick_and_tilde() -> None:
    assert match_fence_marker("```bash") == ("```", "bash")
    assert match_fence_marker("~~~text") == ("~~~", "text")
    assert match_fence_marker("````") == ("````", "")


def test_match_fence_marker_none_for_non_fence_line() -> None:
    assert match_fence_marker("not a fence") is None
    assert match_fence_marker("``two backticks") is None


def test_closes_fence_same_marker_at_least_as_long() -> None:
    assert closes_fence("```", "```")
    assert closes_fence("````", "```")  # longer closer is fine
    assert not closes_fence("``", "```")  # shorter closer does not close
    assert not closes_fence("~~~", "```")  # different marker does not close


def test_fence_flags_basic_open_close() -> None:
    """The opener is reported False (not inside); the closer is reported
    True (per :func:`fence_flags`'s own docstring: "the closing one *is*")."""
    lines = ["a", "```", "b", "```", "c"]
    assert fence_flags(lines) == [False, False, True, True, False]


def test_fence_flags_four_backtick_survives_triple_backtick_content() -> None:
    lines = ["````bash", "```", "echo done", "````"]
    assert fence_flags(lines) == [False, True, True, True]


def test_fence_flags_tilde_not_closed_by_backtick() -> None:
    lines = ["~~~bash", "```", "~~~"]
    assert fence_flags(lines) == [False, True, True]


def test_fence_flags_indented_fence() -> None:
    lines = ["- item", "  ```", "  code", "  ```"]
    assert fence_flags(lines) == [False, False, True, True]


def test_fence_flags_closer_with_trailing_content_is_not_a_close() -> None:
    """``fence_flags`` shares :func:`closes_fence` with ``fence_scan``, so a
    marker run carrying trailing content is content, not a close (CommonMark).
    Pins the rule the two consumers must agree on."""
    lines = ["```", "```python", "still inside", "```", "out"]
    assert fence_flags(lines) == [False, True, True, True, False]


def test_fence_flags_unterminated_fence_stays_open() -> None:
    lines = ["```", "still inside"]
    assert fence_flags(lines) == [False, True]

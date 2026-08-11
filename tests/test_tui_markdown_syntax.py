"""Live markdown syntax colouring in the four note-body TextAreas (lode-ev5j.2, lode-ngk2).

Covers the shared helper :func:`lode.tui.screens._markdown_area._markdown_text_area`:
graceful degradation when the tree-sitter markdown grammar is unavailable, that each of
the four named screens' body ``TextArea`` actually constructs with
``language="markdown"`` (and that :class:`~lode.tui.screens.reconcile.ReconcileScreen`'s
diff view, deliberately excluded, does not), and that the built-in highlighter's private
``_highlights`` channel holds the expected entries for a fixture buffer covering the
agreed stock block-grammar token set (headings, heading markers, fenced code, fence
delimiters, list markers, block-quote markers, thematic breaks, backslash escapes, and
reference-style links -- lode-ev5j.1's spike found inline ``[text](url)`` links
unreachable on this grammar, so they're deliberately not asserted here).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from rich.style import Style
from rich.text import Text
from textual.screen import Screen
from textual.widgets import TextArea
from textual.widgets.text_area import LanguageDoesNotExist

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens._markdown_area import (
    NOTE_BODY_SYNTAX_STYLES,
    NOTE_BODY_THEME,
    _markdown_text_area,
)
from lode.tui.screens.capture import BODY_ID, CaptureScreen
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.reconcile import DIFF_ID, ReconcileScreen
from lode.tui.screens.snapshot_viewer import (
    SNAPSHOT_VIEWER_BODY_ID,
    SnapshotViewerScreen,
)
from lode.tui.screens.version_view import VERSION_BODY_ID, VersionViewScreen
from lode.versions import save

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pushed_screen_language(app: LodeApp, screen: Screen, widget_id: str) -> str | None:
    """Push ``screen`` onto ``app`` and read back its ``TextArea``'s language.

    Shared by the three ``push_screen``-shaped cases below (the EditScreen case
    drives the keyboard instead, so it keeps its own driver).
    """

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            return app.screen.query_one(f"#{widget_id}", TextArea).language

    return asyncio.run(_drive())


# ---------------------------------------------------------------------------
# textual[syntax] is a HARD dependency (pyproject.toml), not an optional extra.
# ---------------------------------------------------------------------------


def test_textual_syntax_extra_is_a_hard_dependency() -> None:
    """``textual[syntax]`` -- not the plain, ungrammared ``textual`` -- is declared."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()

    assert 'textual[syntax]"' in pyproject


def test_tree_sitter_markdown_grammar_is_importable() -> None:
    """The grammar package ``textual[syntax]`` pulls in actually resolves."""
    import tree_sitter_markdown  # noqa: F401


# ---------------------------------------------------------------------------
# Graceful degradation (missing/broken grammar simulated via monkeypatch).
# ---------------------------------------------------------------------------


def _break_grammar(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make ``TextArea(..., language=...)`` raise ``exc``, leaving plain construction alone.

    Both broken-environment arms differ only in which exception comes out of
    ``TextArea.__init__``, so the simulation lives here once rather than being
    re-typed per case.
    """
    real_init = TextArea.__init__

    def _fail_if_language_requested(
        self: TextArea, *args: object, **kwargs: object
    ) -> None:
        if kwargs.get("language") is not None:
            raise exc
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(TextArea, "__init__", _fail_if_language_requested)


def test_markdown_text_area_falls_back_to_plain_text_when_grammar_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the grammar being absent: no raise, plain uncoloured TextArea."""
    _break_grammar(monkeypatch, LanguageDoesNotExist("simulated missing grammar"))

    widget = _markdown_text_area("some body", id="body", read_only=True)

    assert widget.language is None
    assert widget.text == "some body"
    assert widget.read_only is True
    assert widget.id == "body"


def test_markdown_text_area_falls_back_when_grammar_abi_is_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The *other* broken-env path: ``tree_sitter.Language()`` raises ValueError.

    ``textual._tree_sitter.get_language`` catches ImportError/OSError/AttributeError
    and returns None (which Textual turns into ``LanguageDoesNotExist``), but it
    does **not** catch the ``ValueError`` that ``tree_sitter.Language()`` raises
    when the grammar's compiled ABI and the installed ``tree-sitter`` core
    disagree -- so that one propagates straight out of ``TextArea.__init__``.
    With deps deliberately unpinned (pyproject.toml), an independently-resolved
    ``tree-sitter`` / ``tree-sitter-markdown`` pair makes this the more likely of
    the two failures, so the helper must degrade here too rather than kill the
    screen.
    """
    _break_grammar(monkeypatch, ValueError("invalid language ID"))

    widget = _markdown_text_area("some body", id="body", read_only=True)

    assert widget.language is None
    assert widget.text == "some body"
    assert widget.read_only is True
    assert widget.id == "body"


def test_markdown_text_area_does_not_swallow_a_malformed_widget_id() -> None:
    """The ValueError arm stays narrow: a real construction bug still raises.

    ``textual.dom.BadIdentifier`` derives directly from ``Exception``, not from
    ``ValueError``, so broadening the guard above does not mask it.
    """
    from textual.dom import BadIdentifier

    with pytest.raises(BadIdentifier):
        _markdown_text_area("some body", id="not a valid id!")


def test_markdown_text_area_uses_markdown_language_when_grammar_present() -> None:
    widget = _markdown_text_area("some body", id="body")

    assert widget.language == "markdown"
    assert widget.text == "some body"


# ---------------------------------------------------------------------------
# Fenced-code-block colour (lode-lab1). MAINTAINER DECISION (lode-lab1 notes):
# magenta, colour only.
# ---------------------------------------------------------------------------


def test_note_body_syntax_styles_is_exactly_text_literal_magenta() -> None:
    """The whole declared palette, asserted as one equality.

    ``Style`` equality covers every attribute at once, so this pins "magenta,
    colour only" (no bold, no background tint) *and* pins that ``"none"`` stays
    unmapped -- lode-76go found that capture is emitted later in each line's
    highlight iteration order, so mapping it would win the colour attribute at
    render time and silently undo ``text.literal``.
    """
    assert NOTE_BODY_SYNTAX_STYLES == {"text.literal": Style(color="magenta")}


def test_markdown_text_area_applies_the_shared_note_body_theme() -> None:
    """A successfully-graded ``TextArea`` is registered with, and set to, our theme."""
    widget = _markdown_text_area("some body", id="body")

    assert widget.theme == NOTE_BODY_THEME.name
    assert NOTE_BODY_THEME.name in widget.available_themes


def test_fenced_code_block_lines_render_magenta_end_to_end() -> None:
    """The point of the whole ticket: fenced-block lines colour, prose does not.

    Asserting the palette and the wiring separately would both stay green if a
    Textual upgrade renamed the capture out from under us, so this drives the
    real highlighter over a real buffer and resolves the spans exactly the way
    ``TextArea._render_line`` does (look up each capture in the active theme's
    ``syntax_styles``; skip it entirely when unmapped).
    """
    body = "intro\n```python\ndef foo():\n    return 1\n```\ntail\n"
    widget = _markdown_text_area(body, id="body")
    styles = widget._theme.syntax_styles

    def _spans(line_index: int) -> list[Style]:
        text = Text(widget.document.get_line(line_index), end="", no_wrap=True)
        for start, end, capture in widget._highlights[line_index]:
            style = styles.get(capture)
            if style is not None:
                text.stylize(style, start, end)
        return [span.style for span in text.spans]

    magenta = Style(color="magenta")
    # The opening delimiter, both body lines, and the closing delimiter.
    for line_index in (1, 2, 3, 4):
        assert _spans(line_index) == [magenta], f"line {line_index} not coloured"
    # Surrounding prose is left alone.
    assert _spans(0) == []
    assert _spans(5) == []


def test_markdown_text_area_fallback_does_not_touch_theme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-grammar fallback stays a plain TextArea -- no third failure mode."""
    _break_grammar(monkeypatch, LanguageDoesNotExist("simulated missing grammar"))

    widget = _markdown_text_area("some body", id="body")

    assert NOTE_BODY_THEME.name not in widget.available_themes


# ---------------------------------------------------------------------------
# All four named screens construct their body TextArea with language="markdown";
# reconcile.py (a diff view, not markdown) stays untouched.
# ---------------------------------------------------------------------------


def test_capture_screen_body_is_markdown_language(tmp_path: Path) -> None:
    """CaptureScreen (lode-ngk2) -- the screen where a user actually types.

    Also pins the placeholder: routing this body through the shared helper is
    what put it there, so a later signature change that quietly dropped it
    would otherwise land uncaught.
    """
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str | None, str]:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            body = app.screen.query_one(f"#{BODY_ID}", TextArea)
            return body.language, str(body.placeholder)

    assert asyncio.run(_drive()) == ("markdown", "What did you learn today?")


def test_edit_screen_body_is_markdown_language(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "the note body")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            return app.screen.query_one(f"#{EDIT_BODY_ID}", TextArea).language

    assert asyncio.run(_drive()) == "markdown"


def test_version_view_screen_body_is_markdown_language(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        version_id = save(conn, "note-a", "a prior version's body").version_id
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    language = _pushed_screen_language(
        app, VersionViewScreen("note-a", version_id), VERSION_BODY_ID
    )

    assert language == "markdown"


def _insert_snapshot(conn: sqlite3.Connection, *, snapshot_id: str, body: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
            ("https://example.com/article", "web"),
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
            "VALUES (?, ?, ?, 'ok')",
            (snapshot_id, "https://example.com/article", body),
        )


def test_snapshot_viewer_screen_body_is_markdown_language(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        _insert_snapshot(conn, snapshot_id="snap-1", body="the extracted article")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    language = _pushed_screen_language(
        app, SnapshotViewerScreen("snap-1"), SNAPSHOT_VIEWER_BODY_ID
    )

    assert language == "markdown"


def test_reconcile_screen_diff_view_is_not_markdown_language(tmp_path: Path) -> None:
    """Explicitly excluded (this class's own docstring): a diff, not markdown."""
    from lode.tui.services.reconcile import Conflict

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    conflict = Conflict(
        note_id="note-a",
        expected_parent="v-old",
        rejected_buffer="my body",
        actual_head="v-new",
        actual_head_body="their body",
        draft_path=tmp_path / "draft.md",
    )
    app = LodeApp(db_path=db_path)

    language = _pushed_screen_language(app, ReconcileScreen(conflict), DIFF_ID)

    assert language is None


# ---------------------------------------------------------------------------
# _highlights holds the expected entries for a fixture buffer covering the
# agreed stock block-grammar token set (empirically verified against the
# installed textual 8.2.8 + tree-sitter-markdown 0.5.1, lode-ev5j.1's spike
# environment). Interactions between adjacent block constructs (e.g. a code
# fence sharing punctuation.special/none captures with a following list) are
# real, observed behaviour of Textual's bundled markdown.scm query -- this
# fixture doesn't try to isolate every construct onto its own line, just to
# assert the presence of the entries the agreed token set requires.
# ---------------------------------------------------------------------------

_FIXTURE = (
    "# Heading\n"
    "\n"
    "```python\n"
    "code\n"
    "```\n"
    "\n"
    "- item\n"
    "\n"
    "> quote\n"
    "\n"
    "---\n"
    "\n"
    "[la\\*bel]: https://example.com\n"
)


#: (line, expected entry, which construct on that line it stands for). Keyed
#: to ``_FIXTURE`` above.
_EXPECTED_HIGHLIGHTS = [
    (0, (0, 1, "heading.marker"), "heading marker"),
    (0, (2, 9, "heading"), "heading text"),
    (2, (0, 3, "punctuation.delimiter"), "opening code fence"),
    (4, (0, 3, "punctuation.delimiter"), "closing code fence"),
    (3, (0, None, "text.literal"), "fenced code content"),
    (6, (0, 2, "list.marker"), "list marker"),
    (8, (0, 2, "punctuation.special"), "block-quote marker"),
    (10, (0, None, "list.marker"), "thematic break"),
    (12, (0, 9, "link.label"), "reference-link label"),
    (12, (3, 5, "string.escape"), "backslash escape"),
    (12, (11, 30, "link.uri"), "reference-link URI"),
]


def test_highlights_cover_the_agreed_stock_token_set() -> None:
    """Canary over Textual's bundled ``markdown.scm``, not over lode's own code.

    The exact ``(start_col, end_col, capture)`` entries below were derived
    empirically against textual 8.2.8 / tree-sitter-markdown 0.5.1 and are the
    approach the ticket settled on (asserting the private ``_highlights``, in
    preference to adding a ``pytest-textual-snapshot`` dev dep). Because those
    deps are deliberately **unpinned**, a grammar or query bump upstream can
    legitimately rename a capture or shift a span -- so each assertion reports
    what the grammar actually produced. A failure here means "the upstream
    markdown grammar changed", NOT "lode is broken": re-derive the expectations
    and confirm the token set is still covered before treating it as a defect.
    """
    text_area = _markdown_text_area(_FIXTURE, id="fixture")

    highlights = text_area._highlights

    for line, entry, construct in _EXPECTED_HIGHLIGHTS:
        assert entry in highlights[line], (
            f"{construct}: expected {entry} on line {line} of the fixture, but the "
            f"installed grammar produced {sorted(highlights[line])}. If a textual / "
            f"tree-sitter-markdown bump changed this, re-derive _EXPECTED_HIGHLIGHTS "
            f"rather than assuming a lode regression."
        )

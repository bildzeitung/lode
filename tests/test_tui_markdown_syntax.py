"""Live markdown syntax colouring in the three note-body TextAreas (lode-ev5j.2).

Covers the shared helper :func:`lode.tui.screens._markdown_area.markdown_text_area`:
graceful degradation when the tree-sitter markdown grammar is unavailable, that
each of the three named screens' body ``TextArea`` actually constructs with
``language="markdown"`` (and that :class:`~lode.tui.screens.reconcile.
ReconcileScreen`'s diff view, deliberately excluded, does not), and that the
built-in highlighter's private ``_highlights`` channel holds the expected
entries for a fixture buffer covering the agreed stock block-grammar token set
(headings, heading markers, fenced code, fence delimiters, list markers,
block-quote markers, thematic breaks, backslash escapes, and reference-style
links -- lode-ev5j.1's spike found inline ``[text](url)`` links unreachable on
this grammar, so they're deliberately not asserted here).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from textual.widgets import TextArea
from textual.widgets.text_area import LanguageDoesNotExist

from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens._markdown_area import markdown_text_area
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.reconcile import DIFF_ID, ReconcileScreen
from lode.tui.screens.snapshot_viewer import (
    SNAPSHOT_VIEWER_BODY_ID,
    SnapshotViewerScreen,
)
from lode.tui.screens.version_view import VERSION_BODY_ID, VersionViewScreen
from lode.versions import save

# ---------------------------------------------------------------------------
# textual[syntax] is a HARD dependency (pyproject.toml), not an optional extra.
# ---------------------------------------------------------------------------


def test_textual_syntax_extra_is_a_hard_dependency() -> None:
    """``textual[syntax]`` -- not the plain, ungrammared ``textual`` -- is declared."""
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()

    assert 'textual[syntax]"' in pyproject


def test_tree_sitter_markdown_grammar_is_importable() -> None:
    """The grammar package ``textual[syntax]`` pulls in actually resolves."""
    import tree_sitter_markdown  # noqa: F401


# ---------------------------------------------------------------------------
# Graceful degradation (missing/broken grammar simulated via monkeypatch).
# ---------------------------------------------------------------------------


def test_markdown_text_area_falls_back_to_plain_text_when_grammar_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the grammar being absent: no raise, plain uncoloured TextArea."""
    real_init = TextArea.__init__

    def _raise_missing_language(
        self: TextArea, *args: object, **kwargs: object
    ) -> None:
        if kwargs.get("language") is not None:
            raise LanguageDoesNotExist("simulated missing markdown grammar")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(TextArea, "__init__", _raise_missing_language)

    widget = markdown_text_area("some body", id="body", read_only=True)

    assert widget.language is None
    assert widget.text == "some body"
    assert widget.read_only is True
    assert widget.id == "body"


def test_markdown_text_area_uses_markdown_language_when_grammar_present() -> None:
    widget = markdown_text_area("some body", id="body")

    assert widget.language == "markdown"
    assert widget.text == "some body"


# ---------------------------------------------------------------------------
# All three named screens construct their body TextArea with language="markdown";
# reconcile.py (a diff view, not markdown) stays untouched.
# ---------------------------------------------------------------------------


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

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            app.push_screen(VersionViewScreen("note-a", version_id))
            await pilot.pause()
            return app.screen.query_one(f"#{VERSION_BODY_ID}", TextArea).language

    assert asyncio.run(_drive()) == "markdown"


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

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            app.push_screen(SnapshotViewerScreen("snap-1"))
            await pilot.pause()
            return app.screen.query_one(
                f"#{SNAPSHOT_VIEWER_BODY_ID}", TextArea
            ).language

    assert asyncio.run(_drive()) == "markdown"


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

    async def _drive() -> str | None:
        async with app.run_test() as pilot:
            app.push_screen(ReconcileScreen(conflict))
            await pilot.pause()
            return app.screen.query_one(f"#{DIFF_ID}", TextArea).language

    assert asyncio.run(_drive()) is None


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


def test_highlights_cover_the_agreed_stock_token_set() -> None:
    text_area = markdown_text_area(_FIXTURE, id="fixture")

    highlights = text_area._highlights

    # Heading + heading marker (line 0: "# Heading").
    assert (0, 1, "heading.marker") in highlights[0]
    assert (2, 9, "heading") in highlights[0]
    # Fenced code: fence delimiters (lines 2 and 4) and content (line 3).
    assert (0, 3, "punctuation.delimiter") in highlights[2]
    assert (0, 3, "punctuation.delimiter") in highlights[4]
    assert (0, None, "text.literal") in highlights[3]
    # List marker (line 6: "- item").
    assert (0, 2, "list.marker") in highlights[6]
    # Block-quote marker (line 8: "> quote").
    assert (0, 2, "punctuation.special") in highlights[8]
    # Thematic break (line 10: "---").
    assert (0, None, "list.marker") in highlights[10]
    # Reference-style link definition + a backslash escape in its label
    # (line 12: "[la\*bel]: https://example.com").
    assert (0, 9, "link.label") in highlights[12]
    assert (3, 5, "string.escape") in highlights[12]
    assert (11, 30, "link.uri") in highlights[12]

"""Screen-level tests for the Ctrl+N open-link-under-cursor binding (lode-ev5j.3, lode-5ill).

Covers the four screens that share this binding --
:class:`~lode.tui.screens.edit.EditScreen`,
:class:`~lode.tui.screens.version_view.VersionViewScreen`,
:class:`~lode.tui.screens.snapshot_viewer.SnapshotViewerScreen` (the three
`lode-ev5j.2`/`lode-ev5j.3` originally targeted), and
:class:`~lode.tui.screens.capture.CaptureScreen` (added by `lode-5ill` once
`lode-ngk2` made it a colouring screen too) -- driven end to end via
Textual's ``run_test`` pilot, the same style every other TUI screen test in
this suite uses. The pure extraction/guard logic itself is covered in
isolation by ``tests/test_link_open.py``; these tests exist to confirm the
Ctrl+N binding is wired up correctly on each screen and that
``webbrowser.open`` / the status-line notification fire as the ticket's
acceptance criteria require.
"""

from __future__ import annotations

import asyncio
import sqlite3
import webbrowser
from pathlib import Path

import pytest

from lode.config import Settings
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID as CAPTURE_BODY_ID
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.screens.snapshot_viewer import (
    SNAPSHOT_VIEWER_BODY_ID,
    SnapshotViewerScreen,
)
from lode.tui.screens.version_view import VERSION_BODY_ID, VersionViewScreen
from lode.versions import save


class _FakeGuiBrowser:
    """A stand-in safe (non-`GenericBrowser`) controller (lode-ev5j.3).

    ``open_link_under_cursor`` now resolves the live ``webbrowser.get()``
    itself (the browser-safety review's controller-type guard), so these
    screen-level tests must control what that resolves to directly, rather
    than relying on whatever browsers happen to be registered on the machine
    actually running the suite.
    """


class _StubEmbedder:
    """Deterministic offline stand-in for the related-notes query embedder (lode-fr3p).

    These tests only exercise the Ctrl+N open-link binding and never assert on
    ``RelatedNotesPanel`` results -- but they cannot simply ignore the panel.
    ``EditScreen.on_mount`` sets the body ``TextArea``'s ``.text`` to the
    just-loaded note (and the capture-screen test sets it directly), and either
    one posts a real ``TextArea.Changed`` that arms the panel's debounce
    (:meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel.update_draft`,
    default 500ms). Whether that timer fires before a test finishes is a
    wall-clock race the test bodies do not control; when it does fire, the
    worker (``RelatedNotesPanel._search_related``) lazily constructs a real
    :class:`~lode.embedding.FastEmbedEmbedder`, whose first embed downloads/loads
    the actual ONNX weights (hundreds of MB on a cold cache) via ``fastembed``.
    At the time this test was written that first embed *also* resolved the
    model's HF revision over a live HTTPS call to huggingface.co
    (:func:`lode.embedding.resolve_model_revision`), unconditionally, whether or
    not the model was already cached on disk -- that call is what
    ``tests/conftest.py``'s socket guard was catching here. lode-dj6m has since
    moved that probe off the query-only path entirely (``embed_query`` no
    longer resolves the revision at all), but the stub is still required: the
    real weights download/construction remains real disk/network/CPU cost no
    test here wants to pay, and a green local run against an already-warm
    cache is *not* evidence this stub is unnecessary.

    Only ``embed_query`` is exercised; the width follows ``settings`` so the
    query vector matches the LanceDB table that ``vector_search`` lazily creates
    for these tests' otherwise-never-written db.
    """

    def __init__(self, settings: Settings) -> None:
        self._dim = settings.embedding_vector_dim

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0] * self._dim


def _stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the default ONNX embedder for :class:`_StubEmbedder` (no network).

    Called by every test below that reaches ``EditScreen`` or sets
    ``CaptureScreen``'s body text -- i.e. exactly the ones that arm the
    related-notes debounce. The ``VersionViewScreen``/``SnapshotViewerScreen``
    tests compose no ``RelatedNotesPanel`` and deliberately do not call it, so
    the presence of this call is itself the signal for which screens surface
    related notes.
    """
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _StubEmbedder)


def _insert_snapshot(
    conn: sqlite3.Connection, *, external_id: str, snapshot_id: str, body: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) VALUES (?, ?, ?)",
            (external_id, "web", 0),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, raw_payload, status, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, external_id, body, None, "ok", "2026-07-08T00:00:00.000000Z"),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )


# ---------------------------------------------------------------------------
# EditScreen
# ---------------------------------------------------------------------------


def test_edit_screen_ctrl_n_on_a_link_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("BROWSER", raising=False)
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "see [my link](https://example.com/path) please")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.get",
        lambda *a, **kw: _FakeGuiBrowser(),
    )
    _stub_embedder(monkeypatch)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.move_cursor((0, 8))  # inside "my link"
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_drive())

    assert opened == ["https://example.com/path"]
    assert any("https://example.com/path" in message for message in messages)


def test_edit_screen_ctrl_n_with_generic_browser_controller_does_not_open_but_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard is an exact controller-TYPE check now (lode-ev5j.3's
    # browser-safety review superseded the original `$BROWSER` name list) --
    # what makes a controller unsafe is resolving to `webbrowser.GenericBrowser`
    # itself, independent of what (if anything) `$BROWSER` names.
    monkeypatch.setenv("DISPLAY", ":0")
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "see [my link](https://example.com/path) please")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.get",
        lambda *a, **kw: webbrowser.GenericBrowser("w3m"),
    )
    _stub_embedder(monkeypatch)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.move_cursor((0, 8))
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_drive())

    assert opened == []  # never handed to a terminal browser -- would corrupt the TUI
    assert any("https://example.com/path" in message for message in messages)


def test_edit_screen_ctrl_n_headless_notifies_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "see [my link](https://example.com/path) please")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    # A perfectly safe, GUI-capable controller -- proves the refusal below is
    # driven by the missing display, not by the controller type.
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.get",
        lambda *a, **kw: _FakeGuiBrowser(),
    )
    _stub_embedder(monkeypatch)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.move_cursor((0, 8))
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_drive())

    assert opened == []
    assert any("https://example.com/path" in message for message in messages)


def test_edit_screen_ctrl_n_with_no_link_under_cursor_notifies_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-a", "no links on this line at all")
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    _stub_embedder(monkeypatch)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            text_area.move_cursor((0, 3))
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return isinstance(app.screen, EditScreen)

    still_edit_screen = asyncio.run(_drive())

    assert opened == []
    assert any("no link under the cursor" in message for message in messages)
    # No crash, no silence -- still on the same screen with a clear message.
    assert still_edit_screen


# ---------------------------------------------------------------------------
# VersionViewScreen
# ---------------------------------------------------------------------------


def test_version_view_screen_ctrl_n_on_a_link_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("BROWSER", raising=False)
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        version_id = save(
            conn, "note-a", "see [my link](https://example.com/path) please"
        ).version_id
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.get",
        lambda *a, **kw: _FakeGuiBrowser(),
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen(VersionViewScreen("note-a", version_id))
            await pilot.pause()
            assert isinstance(app.screen, VersionViewScreen)
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{VERSION_BODY_ID}")
            text_area.move_cursor((0, 8))
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_drive())

    assert opened == ["https://example.com/path"]
    assert any("https://example.com/path" in message for message in messages)


# ---------------------------------------------------------------------------
# SnapshotViewerScreen
# ---------------------------------------------------------------------------


def test_snapshot_viewer_screen_ctrl_n_on_a_link_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("BROWSER", raising=False)
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        _insert_snapshot(
            conn,
            external_id="https://example.com/x",
            snapshot_id="snap-1",
            body="see [my link](https://example.com/path) please",
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.get",
        lambda *a, **kw: _FakeGuiBrowser(),
    )

    async def _drive() -> None:
        async with app.run_test() as pilot:
            app.push_screen(SnapshotViewerScreen("snap-1"))
            await pilot.pause()
            assert isinstance(app.screen, SnapshotViewerScreen)
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{SNAPSHOT_VIEWER_BODY_ID}")
            text_area.move_cursor((0, 8))
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_drive())

    assert opened == ["https://example.com/path"]
    assert any("https://example.com/path" in message for message in messages)


def test_snapshot_viewer_screen_footer_shows_the_link_binding(
    tmp_path: Path,
) -> None:
    """lode-ev5j.3 gave this screen a footer for the first time (see this
    module's own module docstring for why -- it was footerless before, with
    nowhere to show its pre-existing Back/Toggle bindings either)."""
    from textual.widgets import Footer
    from textual.widgets._footer import FooterKey

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        _insert_snapshot(
            conn,
            external_id="https://example.com/x",
            snapshot_id="snap-1",
            body="body text",
        )
    finally:
        conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> list[str]:
        async with app.run_test() as pilot:
            app.push_screen(SnapshotViewerScreen("snap-1"))
            await pilot.pause()
            footer = app.screen.query_one(Footer)
            keys = [c for c in footer.children if isinstance(c, FooterKey)]
            return [c.description for c in keys]

    descriptions = asyncio.run(_drive())

    assert descriptions == ["Back", "Toggle raw HTML", "Link"]


# ---------------------------------------------------------------------------
# CaptureScreen (lode-5ill)
#
# ONE wiring test, deliberately -- the same scope VersionViewScreen and
# SnapshotViewerScreen get above, not the four EditScreen carries. EditScreen
# was first, so it drove the guard scenarios (generic-browser controller,
# headless, no-link-under-cursor) end to end once; those guards live entirely
# inside open_link_under_cursor, which takes (screen, text_area) and so cannot
# behave differently per screen, and each is already covered in isolation in
# tests/test_link_open.py. Re-running them here would assert the same facts a
# third time and pin the guards' wording in one more place. What IS
# screen-specific -- that Ctrl+N is bound and reaches the helper -- is what
# this test covers. CaptureScreen's footer entry is covered by
# test_tui_app.py::test_capture_footer_fits_100_columns_with_every_binding_visible.
# (lode-5ill technical review)
# ---------------------------------------------------------------------------


def test_capture_screen_ctrl_n_on_a_link_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("BROWSER", raising=False)
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)
    messages: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.open",
        lambda url: opened.append(url),
    )
    monkeypatch.setattr(
        "lode.tui.screens._link_open.webbrowser.get",
        lambda *a, **kw: _FakeGuiBrowser(),
    )
    _stub_embedder(monkeypatch)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            assert isinstance(app.screen, CaptureScreen)
            text_area = app.screen.query_one(f"#{CAPTURE_BODY_ID}")
            text_area.text = "see [my link](https://example.com/path) please"
            text_area.move_cursor((0, 8))  # inside "my link"
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_drive())

    assert opened == ["https://example.com/path"]
    assert any("https://example.com/path" in message for message in messages)

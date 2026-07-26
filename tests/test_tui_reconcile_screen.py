"""Screen-level tests for the CAS-conflict reconciliation UI (lode-mkc.4).

Drives the real widgets end to end via Textual's ``run_test`` pilot, the same
style ``tests/test_tui_app.py`` uses for the capture screen: force a CAS
reject with the fixed-uuid collision trick from
``tests/test_tui_capture.py``, then assert the reconcile screen shows a
buffer-vs-head diff and that both resolutions (re-apply / discard) work end
to end against the real ``Repository.save`` CAS path.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from conftest import _wait_until

from lode.hashing import NO_PARENT, content_version_id
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID
from lode.tui.screens.reconcile import DIFF_ID, ReconcileScreen
from lode.tui.services import capture as capture_mod
from lode.versions import save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


# _wait_until moved to tests/conftest.py (lode-lcju) -- see docs/tui.md's
# "Settling TUI tests under load" section for the ruling + mechanism, and
# tests/conftest.py's own docstring for the helper itself.
#
# What the underlying heuristic actually costs these tests, measured
# (lode-64jn review, Textual 8.2.8). Neutralising ``wait_for_idle`` -- i.e.
# modelling a total misfire -- and re-running the pre-fix versions of these
# three tests:
#
# * ``test_cas_reject_shows_a_buffer_vs_head_diff`` **fails**, with
#   ``NoMatches: No nodes match '#reconcile-diff'``. The bare
#   ``pilot.pause()`` returned with :class:`ReconcileScreen` on the stack but
#   its ``compose()`` children not yet mounted -- see :func:`_reconcile_ready`.
#   This test was never named in the ticket and had never been *observed*
#   failing, but it is in fact the most exposed of the three.
# * ``test_reapply_...`` / ``test_discard_...`` **pass** even then -- but only
#   because ``run_test``'s shutdown happens to drain the still-pending
#   keypress, an implicit barrier nothing documents or guarantees. Waiting on
#   the resolution's own observable side effect (the draft's removal) makes
#   that dependency explicit instead of lucky.
#
# **Historical note, so the record is not misread.** The flake actually
# observed in ``test_reapply_saves_onto_new_head_and_exits`` was *not* this
# heuristic: it was the ``ORDER BY created`` version-chain bug, root-caused
# and fixed in lode-t1y (the wall clock can step backward, so two versions
# came back in the wrong order). That is why this file's assertion now reads
# ``ORDER BY rowid``. lode-64jn's other named test,
# ``test_ctrl_s_reset_does_not_schedule_a_stale_related_notes_pass`` (Ctrl+N
# before lode-bsmc consolidated it onto Ctrl+S), was likewise already fixed by
# lode-9vns. The waits below are therefore
# hardening against a real and demonstrated harness race -- not the fix for
# either originally-reported failure, both of which were already resolved
# upstream of this branch.


def _reconcile_ready(app: LodeApp) -> bool:
    """The reconcile screen is up *and composed* -- not merely on the stack.

    ``App.push_screen`` appends to the screen stack synchronously but only
    *posts* the new screen's ``Compose``/``Mount`` messages, so
    ``isinstance(app.screen, ReconcileScreen)`` goes true a beat before
    ``compose()`` has mounted anything. Verified directly against Textual
    8.2.8: in that window ``app.screen.query_one(f"#{DIFF_ID}")`` raises
    ``NoMatches``.

    Gating on the screen type alone would therefore be waiting on a *proxy*
    that can be satisfied early -- precisely the failure mode
    :func:`_wait_until` exists to remove. The diff widget is the real
    precondition for everything these tests do next (read its text; press a
    key whose ``action_reapply`` re-queries it on a renewed conflict), so gate
    on the widget, not on the screen object.
    """
    return isinstance(app.screen, ReconcileScreen) and bool(
        app.screen.query(f"#{DIFF_ID}")
    )


class _FixedUUID:
    """Stand-in so ``str(uuid4())`` yields a chosen note id (forces a collision)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def _seed_collision(db_path: Path, fixed_id: str, original_body: str) -> None:
    conn = init_db(db_path)
    try:
        save(conn, fixed_id, original_body)
    finally:
        conn.close()


def test_cas_reject_shows_a_buffer_vs_head_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    _seed_collision(db_path, fixed_id, "original body")
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+s")
            await _wait_until(
                lambda: _reconcile_ready(app), "the reconcile screen is composed"
            )
            diff_text = app.screen.query_one(f"#{DIFF_ID}").text
            assert "original body" in diff_text
            assert "the conflicting edit" in diff_text

    asyncio.run(_drive())


def test_reapply_saves_onto_new_head_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    _seed_collision(db_path, fixed_id, "original body")
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+s")
            await _wait_until(
                lambda: _reconcile_ready(app), "the reconcile screen is composed"
            )
            # The CAS reject preserved the buffer as a draft before the screen
            # was ever pushed, so one exists now. Pinned explicitly because the
            # wait below polls for its *removal*: were the draft never written,
            # that poll -- and the closing `== []` assertion -- would both pass
            # vacuously, waiting for and asserting nothing.
            assert list(tmp_path.glob("*.draft"))
            await pilot.press("r")
            # reapply() writes the new version FIRST and only then unlinks the
            # draft (lode/tui/reconcile.py), so "no draft on disk" is a genuine
            # postcondition of the save -- not a proxy that could go true before
            # it. Without this wait the test leans on run_test's shutdown to
            # drain the still-pending keypress; see _wait_until's docstring.
            await _wait_until(
                lambda: not list(tmp_path.glob("*.draft")),
                "re-apply has saved and removed the draft",
            )

    asyncio.run(_drive())

    assert app.return_value == fixed_id
    assert _rows(
        db_path,
        "SELECT body, op FROM versions WHERE note_id = ? ORDER BY rowid",
        (fixed_id,),
    ) == [("original body", "create"), ("the conflicting edit", "update")]
    assert list(tmp_path.glob("*.draft")) == []


def test_discard_leaves_head_untouched_and_removes_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    _seed_collision(db_path, fixed_id, "original body")
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+s")
            await _wait_until(
                lambda: _reconcile_ready(app), "the reconcile screen is composed"
            )
            assert list(tmp_path.glob("*.draft"))  # see test_reapply above
            await pilot.press("d")
            # app.return_value stays None on discard (both before AND after
            # action_discard runs), so unlike reapply it can't be the poll
            # signal here -- the draft's removal is the one observable side
            # effect that only becomes true once discard() has actually run.
            await _wait_until(
                lambda: not list(tmp_path.glob("*.draft")),
                "discard has removed the draft",
            )

    asyncio.run(_drive())

    assert app.return_value is None
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (fixed_id,)
    ) == [("original body",)]
    (head,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (fixed_id,)
    )[0]
    assert head == content_version_id(fixed_id, NO_PARENT, "original body")
    assert list(tmp_path.glob("*.draft")) == []

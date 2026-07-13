"""Screen-level tests for Ctrl+N "save and new" on the capture screen (lode-d32.4).

Ctrl+S saves and exits (``tests/test_tui_app.py``); Ctrl+N saves through the
identical no-AI ``save_capture`` path but resets the screen for a fresh note
instead of exiting -- the epic's design fix for "starting a second note means
relaunching the TUI" (specs/04). Drives the real widgets end to end via
Textual's ``run_test`` pilot, the same style ``tests/test_tui_app.py`` and
``tests/test_tui_reconcile_screen.py`` use.

The edge cases the epic's ``/debate`` review flagged (not the ticket's named
CAS-conflict case, which is unreachable in practice -- ``save_capture`` mints a
fresh ``uuid4`` on every call, so it is narrowed once in ``_save_buffer`` and
given one test rather than a flow of its own) get dedicated coverage: clearing
the buffer must not schedule a pointless related-notes pass, and no in-flight
pass may land after the buffer is emptied and paint the previous draft's
results into the cleared panel -- via Ctrl+N's reset *or* via a plain
select-all-delete, which the empty-buffer guard would otherwise strand by
skipping the ``@work(exclusive=True)`` pass that used to supersede it.
"""

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from lode.config import Settings
from lode.storage import init_db
from lode.tui import capture as capture_mod
from lode.tui.app import LodeApp
from lode.tui.related_notes_panel import RelatedNotesPanel
from lode.tui.screens.capture import BODY_ID, RELATED_ID, CaptureScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.versions import save


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_ctrl_n_saves_clears_the_buffer_and_does_not_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the first note of the session"
            await pilot.press("ctrl+n")
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            return app.screen.query_one(f"#{BODY_ID}").text, app.is_running

    body_text, still_running = asyncio.run(_drive())

    assert body_text == ""
    assert still_running
    # Never exited, so app.return_value stays the default (None) -- the note
    # id is not surfaced this way like Ctrl+S's, but the note is persisted.
    assert app.return_value is None
    assert _rows(
        db_path,
        "SELECT body, op FROM versions",
    ) == [("the first note of the session", "create")]


def test_ctrl_n_leaves_focus_in_the_editor(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "focus should stay right here"
            await pilot.press("ctrl+n")
            await pilot.pause()
            return app.screen.query_one(f"#{BODY_ID}").has_focus

    has_focus = asyncio.run(_drive())

    assert has_focus


def test_ctrl_n_lets_a_second_note_be_typed_and_saved_immediately(
    tmp_path: Path,
) -> None:
    """The acceptance criterion end to end: type, Ctrl+N, type again, Ctrl+S."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "first note"
            await pilot.press("ctrl+n")
            await pilot.pause()
            text_area.text = "second note"
            await pilot.press("ctrl+s")

    asyncio.run(_drive())

    assert app.return_value is not None
    assert sorted(row[0] for row in _rows(db_path, "SELECT body FROM versions")) == [
        "first note",
        "second note",
    ]


def test_ctrl_n_on_empty_buffer_refuses_and_does_not_reset(tmp_path: Path) -> None:
    """Mirrors Ctrl+S's empty refusal (``test_saving_an_empty_note_does_not_exit_or_write``)."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)
    still_running = False

    async def _drive() -> None:
        nonlocal still_running
        async with app.run_test() as pilot:
            await pilot.press("ctrl+n")
            still_running = app.is_running

    asyncio.run(_drive())

    assert still_running
    assert not db_path.exists()


def test_ctrl_n_shows_a_saved_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)
    messages: list[str] = []

    async def _drive() -> None:
        async with app.run_test() as pilot:
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "notify me when this lands"
            await pilot.press("ctrl+n")
            await pilot.pause()

    asyncio.run(_drive())

    assert any("Saved" in message for message in messages)


def test_ctrl_n_on_empty_buffer_notifies_the_same_refusal_as_ctrl_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)
    messages: list[str] = []

    async def _drive() -> None:
        async with app.run_test() as pilot:
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            await pilot.press("ctrl+n")
            await pilot.pause()

    asyncio.run(_drive())

    assert messages == ["Refusing to save an empty note."]


class _FixedUUID:
    """Stand-in so ``str(uuid4())`` yields a chosen note id (forces a collision)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def test_ctrl_n_cas_conflict_routes_to_reconcile_screen_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Practically unreachable in normal use (a fresh uuid4 per save never
    collides) but handled identically to Ctrl+S -- forced here the same way
    ``tests/test_tui_reconcile_screen.py`` forces it for Ctrl+S.
    """
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    conn = init_db(db_path)
    try:
        save(conn, fixed_id, "original body")
    finally:
        conn.close()
    monkeypatch.setattr(capture_mod.uuid, "uuid4", lambda: _FixedUUID(fixed_id))
    app = LodeApp(db_path=db_path)
    messages: list[str] = []

    async def _drive() -> str:
        async with app.run_test() as pilot:
            monkeypatch.setattr(
                app, "notify", lambda message, **kw: messages.append(message)
            )
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the conflicting edit"
            await pilot.press("ctrl+n")
            await pilot.pause()
            assert isinstance(app.screen, ReconcileScreen)
            capture = next(s for s in app.screen_stack if isinstance(s, CaptureScreen))
            return capture.query_one(f"#{BODY_ID}").text

    buffer_text = asyncio.run(_drive())

    # No reset: the rejected buffer stands and nothing announces a save that
    # did not happen -- Ctrl+N's reset runs only on a clean save.
    assert buffer_text == "the conflicting edit"
    assert messages == []
    # The original note is untouched (no clobber) -- same guarantee Ctrl+S gives.
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (fixed_id,)
    ) == [("original body",)]


class _CountingStubEmbedder:
    """Offline embedder stand-in that counts constructions (no ONNX download).

    Reused from ``tests/test_tui_app.py``'s convention. ``RelatedNotesPanel.
    _search_related`` (lode-aoc) calls ``_ensure_embedder`` before anything
    else, so a non-zero count is a precise witness that a related-notes pass
    *ran at all*: if
    :meth:`~lode.tui.screens.capture.CaptureScreen.action_save_and_new`'s reset
    failed to stop the pending debounce timer (or the guard in
    ``on_text_area_changed`` failed to skip scheduling one for the now-empty
    buffer), this ticks up. (Constructing the wrapper is itself cheap -- the
    ONNX load is lazy, inside ``embed_query`` -- so what the guard actually
    saves is the pointless worker + thread hop, not a cold load. The real
    hazard the guard must handle is the in-flight pass; see
    ``test_clearing_the_buffer_cancels_an_in_flight_related_notes_worker``.)
    """

    instances = 0

    def __init__(self, settings: Settings) -> None:
        self._dim = settings.embedding_vector_dim
        type(self).instances += 1

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0] * self._dim


def test_ctrl_n_reset_does_not_schedule_a_stale_related_notes_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-9vns: deterministic replacement for a real-wall-clock race.

    The original version of this test armed the debounce indirectly
    (``text_area.text = ...``, which posts a ``Changed`` message Textual
    processes on its own schedule) and raced it against
    ``await pilot.press("ctrl+n")`` -- itself dispatched through the same
    message queue -- hoping a 0.3s pause afterwards was long enough to
    "outlive" the 50ms debounce window regardless of who won. Under
    ``pytest -n auto`` with a cold model cache, the session-scoped
    ``_cache_cross_encoder_model_load`` fixture's real ONNX load can stall
    this worker process's event loop for longer than 50ms *between* those two
    message-queue hops -- the debounce fires for real, before Ctrl+N's
    ``reset()`` ever gets a chance to run, and the test fails on a cold-cache
    timing accident, not a logic bug. Widening the debounce or the
    post-press pause only lowers the odds of losing that race; it does not
    remove it.

    The fix drives both halves directly and synchronously instead: arming
    the real debounce timer via ``panel.update_draft`` (what
    ``on_text_area_changed`` would otherwise post a message to reach) and
    triggering the reset via ``CaptureScreen.action_save_and_new`` directly
    (what the Ctrl+N binding would otherwise post a Key event to reach), with
    no ``await`` between the two calls. asyncio is cooperative and
    single-threaded: a scheduled ``Timer`` callback cannot run without an
    event-loop yield, so with no yield point between arming and cancelling,
    the timer is provably stopped before its deadline regardless of how much
    real time elapses anywhere else in the process. (The Ctrl+N keybinding
    dispatch itself -- not this cancellation behavior -- is already covered
    by this file's other Ctrl+N tests, e.g.
    ``test_ctrl_n_saves_clears_the_buffer_and_does_not_exit``.)

    ``text_area.text = ...`` and ``text_area.clear()`` (inside
    ``action_save_and_new``) each still post their own ``Changed`` message,
    but ``on_text_area_changed`` reads ``event.text_area.text`` lazily
    (current state, not a snapshot) -- by the time either message is
    actually processed, later in this test, the buffer is already the empty
    string ``clear()`` left it as, so ``update_draft("")`` just clears the
    panel again rather than re-arming a stray timer.
    """
    db_path = tmp_path / "lode.db"
    _CountingStubEmbedder.instances = 0
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _CountingStubEmbedder)
    settings = Settings(related_notes_debounce_ms=50, related_notes_min_chars=0)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            screen = app.screen
            assert isinstance(screen, CaptureScreen)
            text_area = screen.query_one(f"#{BODY_ID}")
            text_area.text = "a note long enough to normally trigger a pass"
            panel = screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            panel.update_draft(text_area.text)
            # The real 50ms debounce timer is now pending -- but nothing has
            # awaited the event loop since it was armed, so it structurally
            # cannot have fired yet.
            screen.action_save_and_new()
            # Cancelled synchronously inside reset(), in the same breath as
            # arming it above -- not a race against the timer's deadline.
            assert panel._related_timer is None
            await pilot.pause()
            await app.workers.wait_for_complete()

    asyncio.run(_drive())

    assert _CountingStubEmbedder.instances == 0


def _slow_find_related_notes(
    db_path, draft, *, settings=None, embedder=None, exclude_note_id=None
):
    """Stand-in for a slow pass, faithful to the real function's short-circuit.

    ``find_related_notes`` returns ``[]`` immediately for a draft shorter than
    ``related_notes_min_chars`` without touching the DB or the embedder, so this
    stub must too -- otherwise a *new* pass on the just-emptied buffer would
    also return the stale result, and a test asserting "no stale result" would
    pass whether or not the in-flight pass was actually cancelled. Accepts
    (and ignores) ``exclude_note_id`` (lode-aoc) purely to match the real
    function's signature -- ``RelatedNotesPanel`` always passes it through.
    """
    del embedder, exclude_note_id
    min_chars = settings.related_notes_min_chars if settings else 20
    if len(draft.strip()) < min_chars:
        return []
    _PASS_STARTED.set()
    time.sleep(0.3)
    from lode.tui.related import RelatedNote

    return [RelatedNote(note_id="stale-note", snippet="stale", age="just now")]


#: Set by :func:`_slow_find_related_notes` once a slow pass is genuinely running.
_PASS_STARTED = threading.Event()


async def _await_slow_pass_start() -> None:
    """Block until the slow pass has started, without starving the event loop.

    Waiting via ``asyncio.to_thread`` (rather than blocking this coroutine
    directly) keeps the event loop free to run the debounce timer callback and
    schedule the worker in the first place -- a direct blocking wait here would
    starve the very event loop that needs to start the pass.
    """
    assert await asyncio.to_thread(_PASS_STARTED.wait, 2.0)


def test_ctrl_n_cancels_an_in_flight_related_notes_worker_before_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/debate`` review's edge case (b): a slow in-flight pass from the
    just-saved note must not land after the reset and paint its stale result
    into the freshly-cleared panel.
    """
    db_path = tmp_path / "lode.db"
    _PASS_STARTED.clear()
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _CountingStubEmbedder)
    monkeypatch.setattr("lode.tui.related.find_related_notes", _slow_find_related_notes)
    settings = Settings(related_notes_debounce_ms=1, related_notes_min_chars=1)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> tuple[list, str]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "typing about something before saving"
            await _await_slow_pass_start()
            await pilot.press("ctrl+n")
            # Outlive the slow pass's 0.3s sleep so a not-actually-cancelled
            # worker would have time to land and repaint the panel.
            await pilot.pause(0.5)
            await app.workers.wait_for_complete()
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            return panel._related, str(panel.content)

    related, panel_text = asyncio.run(_drive())

    assert related == []
    assert "stale" not in panel_text


def test_clearing_the_buffer_cancels_an_in_flight_related_notes_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Emptying the buffer by hand must kill the in-flight pass, like Ctrl+N does.

    Skipping the debounce for an empty buffer means ``@work(exclusive=True)``
    never starts a new pass, and so never supersedes the one already in flight
    for the draft the user just deleted. Without an explicit cancel that pass
    lands afterwards and paints the deleted draft's related notes into the
    emptied panel -- the same hazard Ctrl+N's reset guards against, on the plain
    select-all-delete path.
    """
    db_path = tmp_path / "lode.db"
    _PASS_STARTED.clear()
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _CountingStubEmbedder)
    monkeypatch.setattr("lode.tui.related.find_related_notes", _slow_find_related_notes)
    settings = Settings(related_notes_debounce_ms=1, related_notes_min_chars=1)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> tuple[list, str]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "typing about something interesting"
            await _await_slow_pass_start()
            text_area.text = ""  # select-all, delete -- not Ctrl+N
            await pilot.pause(0.5)
            await app.workers.wait_for_complete()
            panel = app.screen.query_one(f"#{RELATED_ID}", RelatedNotesPanel)
            return panel._related, str(panel.content)

    related, panel_text = asyncio.run(_drive())

    assert related == []
    assert "stale" not in panel_text


def test_on_unmount_cancels_a_pending_related_notes_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-ivu: every exit/navigation path except Ctrl+N's
    ``action_save_and_new`` left the debounce timer running after the screen
    -- and hence this panel -- went away (Ctrl+S save-and-exit, Escape/Ctrl+Q
    discard, a future navigation). ``RelatedNotesPanel.on_unmount`` closes
    that gap generically, at the widget's own lifecycle hook, rather than
    needing a ``reset()`` call duplicated into every exit path of every screen
    that composes this widget (capture *and*
    :class:`~lode.tui.screens.browse.EditScreen`).

    Calls ``on_unmount`` directly rather than driving it through an actual
    screen pop or app exit: Textual's *own* generic per-widget teardown
    (``MessagePump._close_messages`` stopping every timer registered via
    ``set_timer``, dispatched as part of the same removal Textual already
    performs on a real unmount) turns out to already race-free cancel the
    timer in every removal path this test tried empirically (a direct
    ``panel.remove()``, and ``app.exit()`` followed by an immediate
    ``run_test()`` exit) -- with *or without* this ticket's fix, making an
    end-to-end drive through either path non-discriminating. Calling the
    hook directly is the one deterministic way to pin down that this
    specific method does its job: without it (before this fix), this fails
    two ways -- ``AttributeError`` (no such method), and, if simulated by
    skipping the call, the debounce timer surviving to actually run a pass
    after the 50ms window elapses.

    **Audited for lode-9vns's wall-clock race (not affected):** ``update_draft``
    and ``on_unmount`` below are called back-to-back with no ``await`` between
    them, so -- exactly like
    :func:`test_ctrl_n_reset_does_not_schedule_a_stale_related_notes_pass`'s
    fix -- the real timer cannot fire in that gap regardless of real-world
    CPU contention elsewhere; the ``pilot.pause(0.3)`` below only proves the
    cancellation held, it is not "outliving" a race that could still be lost.
    """
    db_path = tmp_path / "lode.db"
    _CountingStubEmbedder.instances = 0
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _CountingStubEmbedder)
    settings = Settings(related_notes_debounce_ms=50, related_notes_min_chars=0)
    app = LodeApp(db_path=db_path, settings=settings)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            panel = app.screen.query_one(RelatedNotesPanel)
            panel.update_draft("a note long enough to normally trigger a pass")
            # The 50ms debounce timer is now pending, not yet fired.
            panel.on_unmount()
            # Outlive the debounce window a not-actually-cancelled timer
            # would still fire on.
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()

    asyncio.run(_drive())

    assert _CountingStubEmbedder.instances == 0

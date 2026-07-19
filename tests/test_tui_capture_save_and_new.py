"""Screen-level tests for Ctrl+S "save and new" on the capture screen (lode-d32.4, lode-bsmc).

Ctrl+S saves through the no-AI ``save_capture`` path and resets the screen for
a fresh note instead of exiting -- the epic's design fix for "starting a
second note means relaunching the TUI" (specs/04). Originally a separate
Ctrl+N binding alongside a Ctrl+S that saved-and-exited; lode-bsmc
consolidated the capture screen onto one stack-aware Ctrl+S (this screen is
always the bottom of the stack, so its Ctrl+S is unconditionally "Save &
New") and freed the Ctrl+N letter -- see
:meth:`~lode.tui.screens.capture.CaptureScreen.action_save` and
``docs/keybindings.md``. Drives the real widgets end to end via Textual's
``run_test`` pilot, the same style ``tests/test_tui_app.py`` and
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


def test_ctrl_s_saves_clears_the_buffer_and_does_not_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[str, bool]:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "the first note of the session"
            await pilot.press("ctrl+s")
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


def test_ctrl_s_leaves_focus_in_the_editor(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "focus should stay right here"
            await pilot.press("ctrl+s")
            await pilot.pause()
            return app.screen.query_one(f"#{BODY_ID}").has_focus

    has_focus = asyncio.run(_drive())

    assert has_focus


def test_ctrl_s_lets_a_second_note_be_typed_and_saved_immediately(
    tmp_path: Path,
) -> None:
    """The acceptance criterion end to end: type, Ctrl+S, type again, Ctrl+S."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)

    async def _drive() -> bool:
        async with app.run_test() as pilot:
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "first note"
            await pilot.press("ctrl+s")
            await pilot.pause()
            text_area = app.screen.query_one(f"#{BODY_ID}")
            text_area.text = "second note"
            await pilot.press("ctrl+s")
            await pilot.pause()
            return app.is_running

    still_running = asyncio.run(_drive())

    # Neither save exits -- both stay in the app (stack-aware "Save & New").
    assert still_running
    assert app.return_value is None
    assert sorted(row[0] for row in _rows(db_path, "SELECT body FROM versions")) == [
        "first note",
        "second note",
    ]


def test_ctrl_s_on_empty_buffer_refuses_and_does_not_reset(tmp_path: Path) -> None:
    """Empty/whitespace buffer: refused, same as any other save attempt."""
    db_path = tmp_path / "lode.db"
    app = LodeApp(db_path=db_path)
    still_running = False

    async def _drive() -> None:
        nonlocal still_running
        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            still_running = app.is_running

    asyncio.run(_drive())

    assert still_running
    assert not db_path.exists()


def test_ctrl_s_shows_a_saved_notification(
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
            await pilot.press("ctrl+s")
            await pilot.pause()

    asyncio.run(_drive())

    assert any("Saved" in message for message in messages)


def test_ctrl_s_on_empty_buffer_notifies_the_refusal(
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
            await pilot.press("ctrl+s")
            await pilot.pause()

    asyncio.run(_drive())

    assert messages == ["Refusing to save an empty note."]


class _FixedUUID:
    """Stand-in so ``str(uuid4())`` yields a chosen note id (forces a collision)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def test_ctrl_s_cas_conflict_routes_to_reconcile_screen_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Practically unreachable in normal use (a fresh uuid4 per save never
    collides) but handled identically to any other save path -- forced here
    the same way ``tests/test_tui_reconcile_screen.py`` forces it.
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
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ReconcileScreen)
            capture = next(s for s in app.screen_stack if isinstance(s, CaptureScreen))
            return capture.query_one(f"#{BODY_ID}").text

    buffer_text = asyncio.run(_drive())

    # No reset: the rejected buffer stands and nothing announces a save that
    # did not happen -- the reset runs only on a clean save.
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
    :meth:`~lode.tui.screens.capture.CaptureScreen.action_save`'s reset
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


def test_ctrl_s_reset_does_not_schedule_a_stale_related_notes_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-9vns: deterministic replacement for a real-wall-clock race.

    The original version of this test armed the debounce indirectly
    (``text_area.text = ...``, which posts a ``Changed`` message Textual
    processes on its own schedule) and raced it against
    ``await pilot.press("ctrl+s")`` -- itself dispatched through the same
    message queue -- hoping a 0.3s pause afterwards was long enough to
    "outlive" the 50ms debounce window regardless of who won. Under
    ``pytest -n auto`` with a cold model cache, the session-scoped
    ``_cache_cross_encoder_model_load`` fixture's real ONNX load can stall
    this worker process's event loop for longer than 50ms *between* those two
    message-queue hops -- the debounce fires for real, before Ctrl+S's
    ``reset()`` ever gets a chance to run, and the test fails on a cold-cache
    timing accident, not a logic bug. Widening the debounce or the
    post-press pause only lowers the odds of losing that race; it does not
    remove it.

    The fix drives both halves directly and synchronously instead: arming
    the real debounce timer via ``panel.update_draft`` (what
    ``on_text_area_changed`` would otherwise post a message to reach) and
    triggering the reset via ``CaptureScreen.action_save`` directly (what the
    Ctrl+S binding would otherwise post a Key event to reach), with no
    ``await`` between the two calls. asyncio is cooperative and
    single-threaded: a scheduled ``Timer`` callback cannot run without an
    event-loop yield, so with no yield point between arming and cancelling,
    the timer is provably stopped before its deadline regardless of how much
    real time elapses anywhere else in the process.

    Driving the action directly means this test no longer presses Ctrl+S, so
    the binding -> action wiring has to be covered elsewhere or a broken
    keybinding would ship green. It is:
    :func:`test_ctrl_s_cancels_an_in_flight_related_notes_worker_before_reset`
    presses the *real* Ctrl+S and asserts the related-notes pass was cancelled
    -- i.e. the whole chain (keybinding -> ``action_save`` ->
    ``RelatedNotesPanel`` cancellation) still has end-to-end coverage; it can
    afford the real keypress because it gates on a ``threading.Event`` the
    pass itself sets, not on a wall-clock deadline. Further tests in this
    file (e.g. ``test_ctrl_s_saves_clears_the_buffer_and_does_not_exit``)
    press Ctrl+S for the save/clear/focus/notify behaviour. What is only
    covered directly, and deliberately so, is the one assertion that cannot be
    made against a real keypress without re-introducing the race: that a
    *pending* (not yet fired) debounce timer is cancelled.

    ``text_area.text = ...`` and ``text_area.clear()`` (inside
    ``action_save``) each still post their own ``Changed`` message, but
    ``on_text_area_changed`` reads ``event.text_area.text`` lazily (current
    state, not a snapshot) -- by the time either message is actually
    processed, later in this test, the buffer is already the empty string
    ``clear()`` left it as, so ``update_draft("")`` just clears the panel
    again rather than re-arming a stray timer.
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
            screen.action_save()
            # Cancelled synchronously inside reset(), in the same breath as it
            # was armed above: nothing awaited the event loop in between, so
            # the timer structurally cannot have fired first. Not a race.
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


def test_ctrl_s_cancels_an_in_flight_related_notes_worker_before_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/debate`` review's edge case (b): a slow in-flight pass from the
    just-saved note must not land after the reset and paint its stale result
    into the freshly-cleared panel.

    **Audited for lode-9vns's wall-clock race (not affected):** the arming half
    is gated on ``_PASS_STARTED`` (a ``threading.Event`` the pass itself sets),
    not on a deadline, so a stalled event loop cannot make this test start
    asserting before the pass exists. The ``pilot.pause(0.5)`` is on the far
    side of the cancel, and losing *that* race is harmless: if a stall let the
    slow pass land before Ctrl+S was processed, ``reset()`` clears the panel
    anyway, so the assertions below still hold. A stall can only cost this test
    discriminating power, never turn it red.
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
            await pilot.press("ctrl+s")
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
    """Emptying the buffer by hand must kill the in-flight pass, like Ctrl+S does.

    Skipping the debounce for an empty buffer means ``@work(exclusive=True)``
    never starts a new pass, and so never supersedes the one already in flight
    for the draft the user just deleted. Without an explicit cancel that pass
    lands afterwards and paints the deleted draft's related notes into the
    emptied panel -- the same hazard Ctrl+S's reset guards against, on the plain
    select-all-delete path.

    **Audited for lode-9vns's wall-clock race (not affected):** same shape as
    :func:`test_ctrl_s_cancels_an_in_flight_related_notes_worker_before_reset`
    above -- ``_PASS_STARTED``-gated arming, and a post-cancel pause whose loss
    can only cost discriminating power, not turn the test red.
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
            text_area.text = ""  # select-all, delete -- not Ctrl+S
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
    """lode-ivu: every exit/navigation path except Ctrl+S's "Save & New"
    ``action_save`` left the debounce timer running after the screen -- and
    hence this panel -- went away (the quit/discard confirm's save-and-exit,
    Escape/Ctrl+Q discard, :class:`~lode.tui.screens.edit.EditScreen`'s Ctrl+S
    save-and-pop, a future navigation). ``RelatedNotesPanel.on_unmount``
    closes that gap generically, at the widget's own lifecycle hook, rather
    than needing a ``reset()`` call duplicated into every exit path of every
    screen that composes this widget (capture *and*
    :class:`~lode.tui.screens.edit.EditScreen`).

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
    :func:`test_ctrl_s_reset_does_not_schedule_a_stale_related_notes_pass`'s
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
            assert panel._related_timer is None
            # The assert above pins the cancellation structurally; this pause
            # additionally outlives the debounce window, so a timer whose
            # handle was dropped without its task actually being stopped would
            # still be caught firing by the instances==0 check below.
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()

    asyncio.run(_drive())

    assert _CountingStubEmbedder.instances == 0

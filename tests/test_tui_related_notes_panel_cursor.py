"""RelatedNotesPanel's growth must never displace the edit cursor line (lode-35nu.10).

Root cause (see ``RelatedNotesPanel.on_mount``'s docstring): the panel is a
``Static`` sibling of the body ``TextArea`` inside a ``Vertical``. ``Static``'s
own ``DEFAULT_CSS`` height is ``auto``, and the ``TextArea``'s is ``1fr`` --
Textual sizes ``auto`` siblings first and hands the *remainder* to any ``1fr``
sibling, so every time a passive related-notes pass renders more results, the
panel's auto height grows on the next layout pass and steals rows from the
already-laid-out ``TextArea``, pushing the cursor's line out of the visible
window. The related-notes list is asynchronous and non-user-initiated; it
must never move the ground under an active edit.

These tests drive the real screens (create path: ``CaptureScreen``; edit
path: ``EditScreen``) with a small terminal, put the cursor on the very last
*visible* line of the body, then trigger a related-notes-panel expansion
directly via the panel's own render path (bypassing the debounce timer and
worker for determinism) -- and assert the body ``TextArea``'s own screen
region is unchanged and the cursor's document row is still within its
visible scroll window, with the cursor's ``cursor_location`` untouched.
"""

import asyncio
from pathlib import Path

from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.capture import BODY_ID as CAPTURE_BODY_ID
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.edit import EDIT_BODY_ID, EditScreen
from lode.tui.services.related import RelatedNote
from lode.tui.widgets.related_notes_panel import RelatedNotesPanel

#: Enough lines that a 24-row terminal cannot show the whole body at once --
#: the precondition for "cursor on the last visible line" to mean anything.
_LONG_BODY = "\n".join(f"line {i}" for i in range(40))

#: The maximum a passive pass can ever render (``Settings.related_notes_limit``
#: default, ``lode.config``) -- the worst-case expansion this fix must absorb.
_MAX_RELATED = [
    RelatedNote(note_id=f"note-{i}", snippet=f"snippet {i}", age="3 days ago")
    for i in range(5)
]


def _cursor_row_visible(text_area) -> bool:
    """Is ``text_area``'s cursor row within its current visible scroll window?"""
    row, _column = text_area.cursor_location
    top = text_area.scroll_offset.y
    return top <= row < top + text_area.size.height


async def _put_cursor_at_bottom_then_expand_panel(pilot, text_area, panel) -> None:
    """Shared body: scroll the cursor to the bottom, expand the panel, assert.

    Both the edit and the capture path funnel through this once the screen
    is already open and the body's text is set -- the only thing that
    differs between the two tests is how each screen/body get there, so the
    assertions live here rather than being handed back through a dict for
    each caller to re-spell identically.
    """
    text_area.move_cursor(text_area.document.end)
    text_area.scroll_cursor_visible()
    await pilot.pause()

    region_before = text_area.region
    cursor_before = text_area.cursor_location
    assert _cursor_row_visible(text_area), (
        "test precondition: cursor must start visible"
    )

    # Trigger the panel's growth directly -- the real debounce timer and
    # worker are irrelevant to this layout question and would only add
    # nondeterminism; this calls the exact rendering path they eventually
    # reach (RelatedNotesPanel._render_related).
    panel._render_related(_MAX_RELATED)
    await pilot.pause()

    assert text_area.region == region_before
    assert text_area.cursor_location == cursor_before
    assert _cursor_row_visible(text_area)


def test_edit_screen_panel_expansion_does_not_displace_the_cursor(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    Repository(conn, CompositeCache([])).save("note-under-edit", _LONG_BODY)
    conn.close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            text_area = app.screen.query_one(f"#{EDIT_BODY_ID}")
            panel = app.screen.query_one(RelatedNotesPanel)
            await _put_cursor_at_bottom_then_expand_panel(pilot, text_area, panel)

    asyncio.run(_drive())


def test_focus_border_neither_clips_the_panel_nor_moves_the_text_area(
    tmp_path: Path,
) -> None:
    """The reserved height must absorb the Ctrl+F focus border too.

    ``lode.tcss``'s ``RelatedNotesPanel:focus`` draws a ``round`` border
    (lode-olmi.9) and Textual's default ``box-sizing`` is ``border-box``, so a
    fixed height counts that border *inside* the box. Reserving only
    ``related_notes_limit + 1`` keeps the box stable but silently clips the
    last two related notes exactly when the panel is focused — which is the
    only time Up/Down can step onto them. Pin both halves at once: focusing
    leaves the body ``TextArea``'s region untouched *and* leaves room for the
    header plus every note line.
    """
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> dict:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            text_area = app.screen.query_one(f"#{CAPTURE_BODY_ID}")
            text_area.text = _LONG_BODY
            panel = app.screen.query_one(RelatedNotesPanel)
            panel._render_related(_MAX_RELATED)
            await pilot.pause()
            region_before = text_area.region
            panel.focus()
            await pilot.pause()
            return {
                "region_before": region_before,
                "region_after": text_area.region,
                "content_height": panel.content_region.height,
            }

    result = asyncio.run(_drive())

    assert result["region_after"] == result["region_before"]
    # One header line + every note line a pass can render, still on screen.
    assert result["content_height"] >= len(_MAX_RELATED) + 1


def test_capture_screen_panel_expansion_does_not_displace_the_cursor(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    app = LodeApp(db_path=db_path)

    async def _drive() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, CaptureScreen)
            text_area = app.screen.query_one(f"#{CAPTURE_BODY_ID}")
            text_area.text = _LONG_BODY
            await pilot.pause()
            panel = app.screen.query_one(RelatedNotesPanel)
            await _put_cursor_at_bottom_then_expand_panel(pilot, text_area, panel)

    asyncio.run(_drive())

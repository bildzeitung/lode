"""Styling gate for the shared Yes/No confirm dialogs (lode-1ip2).

THE DEFECT THIS CLOSES. lode-a50f shipped ``NoEgressClearConfirmScreen``
with no ``lode.tcss`` rule at all, so it rendered as a bare unframed line at
the top-left over the browse table -- caught in technical review, not by a
test. lode-1ip2 removed the per-dialog duplication that made that possible:
every confirm now subclasses
:class:`~lode.tui.screens.yes_no_confirm.YesNoConfirmScreen` and its outer
``Vertical`` carries the shared ``.confirm-dialog`` class, which supplies the
frame *and* a default size. This file is the mechanized guard that the
guarantee actually holds at render time.

**Rendered styles ARE in reach of a pilot test.**
``tests/test_tui_capture_confirm.py``'s docstring records the opposite
belief ("a rendered-style concern outside a pilot test's normal reach") --
true of what that file needed at the time, but ``App.run_test`` does resolve
the stylesheet, so ``widget.styles`` and ``widget.region`` can be asserted
directly. That is what makes this gate possible at all.

**Why a bare App and not LodeApp.** The concern under test is purely
"``lode.tcss`` + the screen's own compose", so the harness loads the real
app stylesheet onto a minimal ``App``. No database, no LodeApp startup path,
nothing that could make a styling regression look like an unrelated failure.

NON-VACUITY. ``test_every_subclass_is_covered_here`` fails if someone adds a
fourth confirm screen without adding it below -- the drift this file exists
to prevent, since an uncovered subclass is exactly how lode-a50f happened.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App

import lode.tui
from lode.tui.screens.delete_confirm import DeleteConfirmScreen
from lode.tui.screens.no_egress_confirm import NoEgressClearConfirmScreen
from lode.tui.screens.save_as_note_confirm import (
    SAVE_AS_NOTE_PREVIEW_ID,
    SaveAsNoteConfirmScreen,
)
from lode.tui.screens.yes_no_confirm import YesNoConfirmScreen

#: The real app stylesheet, by absolute path -- ``CSS_PATH`` resolves
#: relative to the declaring class's module, which is this file, not
#: ``lode/tui/``.
TCSS_PATH = str(Path(lode.tui.__file__).parent / "lode.tcss")

#: Terminal size the harness renders at. Both dimensions matter: the
#: save-as-note dialog is sized in percentages, so the assertions below
#: derive their expectations from these numbers rather than hard-coding a
#: second copy of them.
SCREEN_WIDTH = 100
SCREEN_HEIGHT = 40


class _Harness(App):
    """A minimal app carrying nothing but the real lode stylesheet."""

    CSS_PATH = TCSS_PATH


#: (factory, expected dialog width) for every YesNoConfirmScreen subclass.
#: The delete and no-egress confirms take the shared ``.confirm-dialog``
#: default size; save-as-note overrides it with an id rule.
CONFIRM_SCREENS = [
    pytest.param(DeleteConfirmScreen, 50, id="delete"),
    pytest.param(NoEgressClearConfirmScreen, 50, id="no-egress-clear"),
    pytest.param(
        lambda: SaveAsNoteConfirmScreen("preview body\n" * 200),
        int(SCREEN_WIDTH * 0.8),
        id="save-as-note",
    ),
]


def with_dialog(factory, check) -> None:
    """Push ``factory()`` onto a harness app and run ``check(screen, dialog)``.

    The check runs *inside* the ``run_test`` context on purpose: once the app
    exits, its widgets are unmounted and both ``query_one`` and the resolved
    ``styles``/``region`` this file asserts on are gone. Returning the widgets
    to the test body instead would make every assertion here vacuous or
    erroring, so the callback shape is load-bearing, not decoration.
    """

    async def _run() -> None:
        app = _Harness()
        async with app.run_test(size=(SCREEN_WIDTH, SCREEN_HEIGHT)) as pilot:
            screen = factory()
            app.push_screen(screen)
            await pilot.pause()
            check(screen, screen.query_one(f"#{screen.DIALOG_ID}"))

    asyncio.run(_run())


@pytest.mark.parametrize(("factory", "expected_width"), CONFIRM_SCREENS)
def test_confirm_dialog_renders_framed_and_centered(factory, expected_width):
    """Every confirm is bordered, on the panel background, and centered.

    This is lode-a50f's defect stated as an assertion: an unstyled dialog
    has no border and sits at the top-left.
    """

    def check(screen, dialog) -> None:
        assert dialog.has_class("confirm-dialog"), (
            "the dialog's outer Vertical must carry .confirm-dialog -- that "
            "class is the ONLY thing supplying its frame and default size"
        )
        border_style, _border_color = dialog.styles.border.top
        assert border_style == "thick", f"{type(screen).__name__} rendered unframed"
        assert dialog.styles.padding.top == 1
        assert dialog.styles.padding.left == 2

        assert screen.styles.align_horizontal == "center"
        assert screen.styles.align_vertical == "middle"
        # Centering is what actually keeps it off the top-left corner, so
        # assert the rendered region rather than trusting align alone.
        region = dialog.region
        assert region.x > 0 and region.y > 0, (
            f"{type(screen).__name__} rendered at the top-left corner "
            f"({region.x}, {region.y}) -- lode-a50f's exact symptom"
        )
        assert region.x == pytest.approx((SCREEN_WIDTH - region.width) // 2, abs=1)

    with_dialog(factory, check)


@pytest.mark.parametrize(("factory", "expected_width"), CONFIRM_SCREENS)
def test_confirm_dialog_is_sized_and_never_fills_the_screen(factory, expected_width):
    """A confirm gets a real size, not Textual's ``Vertical`` 1fr default.

    Without ``.confirm-dialog``'s width/height a size-less dialog would
    inherit ``width: 1fr; height: 1fr`` and fill the terminal.
    """

    def check(screen, dialog) -> None:
        assert dialog.region.width == expected_width
        assert dialog.region.width < SCREEN_WIDTH, (
            f"{type(screen).__name__} filled the terminal width -- it is "
            "missing a size and fell back to Textual's Vertical 1fr default"
        )
        assert dialog.region.height < SCREEN_HEIGHT

    with_dialog(factory, check)


def test_save_as_note_id_rule_overrides_the_shared_default_size():
    """The one deviation: an id selector outranks the .confirm-dialog class.

    Pins the specificity this file's tcss comment relies on -- save-as-note
    must get the large 80%/80% popup, not the shared small default, while
    still inheriting the shared frame.
    """

    def check(_screen, dialog) -> None:
        assert dialog.region.width == int(SCREEN_WIDTH * 0.8)
        assert dialog.region.height == int(SCREEN_HEIGHT * 0.8)
        assert dialog.region.width != 50, (
            "save-as-note took the shared .confirm-dialog default size -- its "
            "id rule no longer overrides the class"
        )
        border_style, _ = dialog.styles.border.top
        assert border_style == "thick", "the id override dropped the shared frame"

    with_dialog(lambda: SaveAsNoteConfirmScreen("preview body\n" * 200), check)


def test_save_as_note_keeps_its_preview_pane():
    """The ``_extra_children`` hook still mounts the scrollable preview.

    The refactor moved this out of a hand-rolled ``compose``; the preview is
    the whole point of this dialog, so pin that it survived.
    """

    def check(screen, _dialog) -> None:
        preview = screen.query_one(f"#{SAVE_AS_NOTE_PREVIEW_ID}")
        assert preview.parent.id == "save-as-note-preview-pane"
        assert "the previewed body" in str(preview.render())
        # The prompt is composed by the base, NOT by the override -- a
        # subclass can no longer drop it by forgetting a super() call.
        assert screen.query_one(f"#{screen.MESSAGE_ID}") is not None

    with_dialog(lambda: SaveAsNoteConfirmScreen("the previewed body"), check)


def test_every_subclass_is_covered_here():
    """Anti-drift: a new confirm screen must be added to CONFIRM_SCREENS.

    An uncovered subclass is precisely how lode-a50f shipped unstyled.
    """
    covered = {
        DeleteConfirmScreen,
        NoEgressClearConfirmScreen,
        SaveAsNoteConfirmScreen,
    }
    assert set(YesNoConfirmScreen.__subclasses__()) == covered, (
        "a YesNoConfirmScreen subclass is not covered by this styling gate -- "
        "add it to CONFIRM_SCREENS with its expected width"
    )
    assert len(CONFIRM_SCREENS) == len(covered)


@pytest.mark.parametrize(("factory", "expected_width"), CONFIRM_SCREENS)
def test_confirm_declares_its_class_level_message_and_ids(factory, expected_width):
    """Each subclass supplies the three class attributes the base composes."""

    def check(screen, _dialog) -> None:
        assert screen.MESSAGE and "(Y)es / (N)o" in screen.MESSAGE
        assert screen.DIALOG_ID and screen.MESSAGE_ID
        assert str(screen.query_one(f"#{screen.MESSAGE_ID}").render()) == screen.MESSAGE

    with_dialog(factory, check)

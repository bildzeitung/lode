"""Unit tests for the ``LodeStatic`` seam widget (lode-3dz2).

Same standing as ``test_tui_lode_data_table.py``: the screen-level tests
prove the seam is wired into each screen; this proves the mechanism itself --
``LodeStatic`` defaults ``markup=False`` (unlike stock ``Static``, which
defaults ``True``), so a bare ``str`` containing a literal ``[bracket]``
substring renders unmangled, both at construction and through a later
``.update()`` call -- and that a caller can still opt back into
``markup=True`` explicitly if it genuinely wants Rich markup parsed.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from lode.tui.widgets.lode_static import LodeStatic

_STATIC_ID = "test-static"


def _harness_app(widget: Static) -> App[None]:
    class _HarnessScreen(Screen[None]):
        def compose(self) -> ComposeResult:
            yield widget

    class _HarnessApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(_HarnessScreen())

    return _HarnessApp()


def test_construction_renders_a_bracketed_string_literally() -> None:
    """The default (``markup`` unset) survives a literal ``[bracket]``."""
    app = _harness_app(LodeStatic("reviewed [draft] spec", id=_STATIC_ID))

    async def _drive() -> str:
        async with app.run_test() as pilot:
            widget = pilot.app.screen.query_one(f"#{_STATIC_ID}", LodeStatic)
            return widget.render().plain

    assert asyncio.run(_drive()) == "reviewed [draft] spec"


def test_update_after_construction_also_renders_literally() -> None:
    """``markup=False`` applies to every later ``.update()``, not just init
    content -- the flag is stored once on the widget and reused each time."""
    app = _harness_app(LodeStatic("", id=_STATIC_ID))

    async def _drive() -> str:
        async with app.run_test() as pilot:
            widget = pilot.app.screen.query_one(f"#{_STATIC_ID}", LodeStatic)
            widget.update("[withheld] some target: some note")
            return widget.render().plain

    assert asyncio.run(_drive()) == "[withheld] some target: some note"


def test_markup_can_still_be_opted_back_into_explicitly() -> None:
    """Unlike ``RelatedNotesPanel`` (hardcoded ``markup=False``), ``LodeStatic``
    keeps ``markup`` as an overridable kwarg -- a screen with a fixed,
    developer-authored string that genuinely wants a Rich style tag parsed
    can still ask for it."""
    app = _harness_app(LodeStatic("[bold]bold[/bold]", id=_STATIC_ID, markup=True))

    async def _drive() -> str:
        async with app.run_test() as pilot:
            widget = pilot.app.screen.query_one(f"#{_STATIC_ID}", LodeStatic)
            return widget.render().plain

    assert asyncio.run(_drive()) == "bold"

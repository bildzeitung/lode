"""The shared Textual App shell every E11 screen registers against (lode-mkc.1).

Kept minimal on purpose (CLAUDE.md "simplest solution first"): an ``App``
subclass, one shared ``db_path`` / ``settings`` pair resolved once and read by
screens via ``self.app``, and Textual's own built-in ``SCREENS`` registry as
the registration convention. Adding the remaining E11 screens (ask, passive
connections, CAS-reconcile, config/diagnostics — lode-mkc.2/.3/.4, lode-3r4) is
one new module under :mod:`lode.tui.screens` plus one entry in ``SCREENS``
below; no existing screen's file is touched, so those tickets fan out without
colliding here.

``ReconcileScreen`` (lode-mkc.4) is registered here like every other screen,
but — unlike ``capture``'s no-arg push — it needs a
:class:`~lode.tui.reconcile.Conflict` to show, so callers push a constructed
instance (``push_screen(ReconcileScreen(conflict))``) rather than the bare
name; ``SCREENS`` still carries the class for discoverability and so
``app.SCREENS["reconcile"]`` resolves the same way ``"capture"`` does.

**App-level Ctrl+Q confirm-if-dirty (lode-0wj.8).** Ctrl+Q is bound here with
``priority=True``, which makes it a *global* binding that Textual dispatches
straight to the ``App`` from any screen, bypassing that screen's own
bindings entirely — unlike Escape, no individual screen can intercept or
guard it on its own. Quitting from a screen with unsaved state (currently
only :class:`~lode.tui.screens.capture.CaptureScreen`; later lode-0wj.6's
edit flow) must still confirm first, so the guard has to live here instead:
:meth:`LodeApp.action_quit` asks the *current* screen whether it has
something to lose, via an optional ``confirm_quit()`` method, rather than
hardcoding a check against ``CaptureScreen`` — ask/config/reconcile don't
define it, so they keep quitting immediately.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from lode.config import Settings, default_db_path
from lode.tui.screens.ask import AskScreen
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.config import ConfigScreen
from lode.tui.screens.reconcile import ReconcileScreen


class LodeApp(App[str | None]):
    """lode's TUI shell. Starts on the capture screen; screens do the rest.

    The app owns no capture/retrieval logic itself — it only resolves the
    shared ``db_path`` / ``settings`` once and starts the initial screen.
    ``run()`` returns the exited screen's result (the capture screen exits
    with the saved note id, or ``None`` on discard) via Textual's normal
    ``App.exit(result)`` / ``App.run()`` return-value contract. ``F2`` reaches
    the read-only config/diagnostics screen (lode-3r4) from anywhere; it pops
    back to the previous screen on Escape. Ctrl+Q quits immediately unless the
    current screen has unsaved state to confirm first (lode-0wj.8) — see
    :meth:`action_quit`.
    """

    TITLE = "lode"

    #: Registration convention for every E11 screen: name -> Screen subclass,
    #: pushed via ``push_screen("name")``. Extend this dict, not this class,
    #: when a new screen lands.
    SCREENS = {
        "capture": CaptureScreen,
        "config": ConfigScreen,
        "ask": AskScreen,
        "reconcile": ReconcileScreen,
    }

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f2", "show_config", "Config"),
    ]

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__()
        self.db_path = db_path or default_db_path()
        self.settings = settings or Settings()

    def on_mount(self) -> None:
        self.push_screen("capture")

    def action_show_config(self) -> None:
        """Push the config/diagnostics screen (lode-3r4) on top of the current one."""
        self.push_screen("config")

    def action_quit(self) -> None:
        """Ctrl+Q: quit immediately, unless the current screen has unsaved state.

        Overrides Textual's default ``action_quit`` (a bare ``self.exit()``)
        so a dirty buffer gets the same Save/Discard/Cancel confirm Escape
        already gives it (lode-0wj.1), reused across *any* screen since Ctrl+Q
        is a global binding (see the module docstring). A screen opts in by
        defining ``confirm_quit()``; today only
        :class:`~lode.tui.screens.capture.CaptureScreen` does, so ask/config/
        reconcile — and a clean capture buffer, whose own ``confirm_quit``
        exits immediately — all quit right away.
        """
        confirm_quit = getattr(self.screen, "confirm_quit", None)
        if callable(confirm_quit):
            confirm_quit()
            return
        self.exit()


def run(*, db_path: Path | None = None, settings: Settings | None = None) -> str | None:
    """Entry point wired from ``lode tui`` — start the shell on the capture screen."""
    return LodeApp(db_path=db_path, settings=settings).run()

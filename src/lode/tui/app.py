"""The shared Textual App shell every E11 screen registers against (lode-mkc.1).

Kept minimal on purpose (CLAUDE.md "simplest solution first"): an ``App``
subclass, one shared ``db_path`` / ``settings`` pair resolved once and read by
screens via ``self.app``, and Textual's own built-in ``SCREENS`` registry as
the registration convention. Adding the remaining E11 screens (ask, passive
connections, CAS-reconcile, config/diagnostics — lode-mkc.2/.3/.4, lode-3r4) is
one new module under :mod:`lode.tui.screens` plus one entry in ``SCREENS``
below; no existing screen's file is touched, so those tickets fan out without
colliding here.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from lode.config import Settings, default_db_path
from lode.tui.screens.capture import CaptureScreen


class LodeApp(App[str | None]):
    """lode's TUI shell. Starts on the capture screen; screens do the rest.

    The app owns no capture/retrieval logic itself — it only resolves the
    shared ``db_path`` / ``settings`` once and starts the initial screen.
    ``run()`` returns the exited screen's result (the capture screen exits
    with the saved note id, or ``None`` on discard) via Textual's normal
    ``App.exit(result)`` / ``App.run()`` return-value contract.
    """

    TITLE = "lode"

    #: Registration convention for every E11 screen: name -> Screen subclass,
    #: pushed via ``push_screen("name")``. Extend this dict, not this class,
    #: when a new screen lands.
    SCREENS = {"capture": CaptureScreen}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
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


def run(*, db_path: Path | None = None, settings: Settings | None = None) -> str | None:
    """Entry point wired from ``lode tui`` — start the shell on the capture screen."""
    return LodeApp(db_path=db_path, settings=settings).run()

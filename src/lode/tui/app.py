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

**Browse screen (lode-0wj.5, edit-first navigation lode-olmi.2).** ``Ctrl+B``
reaches :class:`~lode.tui.screens.browse.BrowseScreen` the same "global,
reachable from anywhere" way ``Ctrl+O`` reaches config — a plain, non-priority
``App`` binding, since (unlike Ctrl+Q) no screen needs to intercept it. It
lists every live note (Id | Date | Version | Summary, newest-first);
selecting one pushes its editor directly
(:class:`~lode.tui.screens.browse.EditScreen`), and Escape steps back one
screen at a time (editor -> list -> capture) via each screen's own
``pop_screen``.

**Tags screen (lode-olmi.6).** ``Ctrl+T`` reaches
:class:`~lode.tui.screens.tags.TagsScreen` the same "global, reachable from
anywhere" way as ``Ctrl+O``/``Ctrl+B``. Top panel: every distinct tag,
multi-select; bottom panel: live notes carrying **every** selected tag
(AND/intersection; no selection shows every note). Selecting a note there
pushes :class:`~lode.tui.screens.browse.EditScreen` directly.

**No-function-key policy (lode-juz8.1).** Every App-level and Screen-level
binding in this module (and every screen it registers) uses a ``ctrl+``
combo, never a bare function key — see ``docs/keybindings.md``'s "No
function keys" policy section for the full rationale and the letter-space
trap checklist. This screen's own binding was originally the function key
``F5`` (itself a land-time rekey off a colliding ``F4`` — see
``docs/keybindings.md``'s "Resolved collisions" history for that saga), then
remapped to ``Ctrl+T`` by this ticket along with every other F-key in the
TUI.

**App-level stylesheet (lode-1i8.4).** ``CSS_PATH`` loads
:mod:`lode.tui`'s ``lode.tcss`` — an *external* stylesheet (not
``DEFAULT_CSS``), resolved relative to this module's file by Textual and
shipped with the package via ``[tool.setuptools.package-data]``
(``pyproject.toml``). It is the repo's first TUI stylesheet and the pattern
future screens' styling should extend; today it sizes and centers
:class:`~lode.tui.screens.capture.DiscardConfirmScreen`'s dialog box (see
that module's docstring for why ``ModalScreen``'s built-in dimming already
covers the rest).
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from lode.config import Settings, default_db_path
from lode.tui.screens.ask import AskScreen
from lode.tui.screens.browse import BrowseScreen
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.config import ConfigScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.tui.screens.tags import TagsScreen


class LodeApp(App[str | None]):
    """lode's TUI shell. Starts on the capture screen; screens do the rest.

    The app owns no capture/retrieval logic itself — it only resolves the
    shared ``db_path`` / ``settings`` once and starts the initial screen.
    ``run()`` returns the exited screen's result (the capture screen exits
    with the saved note id, or ``None`` on discard) via Textual's normal
    ``App.exit(result)`` / ``App.run()`` return-value contract. ``Ctrl+O``
    reaches the read-only config/diagnostics screen (lode-3r4) from anywhere;
    it pops back to the previous screen on Escape. ``Ctrl+B`` reaches the
    browse screen (lode-0wj.5) the same way, and ``Ctrl+T`` reaches the tags
    screen (lode-olmi.6; no function keys anywhere in the TUI as of
    lode-juz8.1 — see the module docstring above).
    Ctrl+Q quits immediately unless the current
    screen has unsaved state to confirm first (lode-0wj.8) — see
    :meth:`action_quit`.
    """

    TITLE = "lode"
    CSS_PATH = "lode.tcss"

    #: Registration convention for every E11 screen: name -> Screen subclass,
    #: pushed via ``push_screen("name")``. Extend this dict, not this class,
    #: when a new screen lands.
    SCREENS = {
        "capture": CaptureScreen,
        "config": ConfigScreen,
        "ask": AskScreen,
        "reconcile": ReconcileScreen,
        "browse": BrowseScreen,
        "tags": TagsScreen,
    }

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        # "Cfg" stays abbreviated (lode-uczx, amending lode-l38d.3's original
        # rationale). lode's minimum supported terminal width is 100 columns
        # (docs/tui.md), not 80 -- so "buy width for one screen" is no longer
        # the reason: at 100 columns every one of the ten footer-bearing
        # screens fits fine with "Config" spelled out. The real constraint is
        # width reserved for lode-11io's not-yet-landed App-level Ask binding
        # (ctrl+l), measured to cost +7 columns wherever it lands, since an
        # App-level binding renders in every screen's footer. EditScreen is
        # the tightest of the ten (see lode.tui.screens.browse.EditScreen):
        # with "Cfg" it lands at 90/100 today and 97/100 once Ask lands; with
        # "Config" it lands at 93/100 today but 100/100 -- zero slack -- once
        # Ask lands. "Browse"/"Tags" already stay full words; they aren't the
        # constraint. Do not restore "Config" without re-measuring
        # EditScreen's footer against the Ask binding's real cost.
        Binding("ctrl+o", "show_config", "Cfg"),
        Binding("ctrl+b", "show_browse", "Browse"),
        Binding("ctrl+t", "show_tags", "Tags"),
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

    def action_show_browse(self) -> None:
        """Push the browse screen (lode-0wj.5) on top of the current one."""
        self.push_screen("browse")

    def action_show_tags(self) -> None:
        """Push the tags screen (lode-olmi.6) on top of the current one."""
        self.push_screen("tags")

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

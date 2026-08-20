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
:class:`~lode.tui.services.reconcile.Conflict` to show, so callers push a constructed
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
(:class:`~lode.tui.screens.edit.EditScreen`), and Escape steps back one
screen at a time (editor -> list -> capture) via each screen's own
``pop_screen``.

**Tags screen (lode-olmi.6).** ``Ctrl+T`` reaches
:class:`~lode.tui.screens.tags.TagsScreen` the same "global, reachable from
anywhere" way as ``Ctrl+O``/``Ctrl+B``. Top panel: every distinct tag,
multi-select; bottom panel: live notes carrying **every** selected tag
(AND/intersection; no selection shows every note). Selecting a note there
pushes :class:`~lode.tui.screens.edit.EditScreen` directly.

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
:class:`~lode.tui.screens.discard_confirm.DiscardConfirmScreen`'s dialog box
(see that module's docstring for why ``ModalScreen``'s built-in dimming
already covers the rest).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.binding import Binding

from lode.config import Settings, default_db_path
from lode.theming import resolve_note_body_theme, resolve_theme
from lode.tui.screens.ask import AskScreen
from lode.tui.screens.browse import BrowseScreen
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.config import ConfigScreen
from lode.tui.screens.help import HelpScreen
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
    lode-juz8.1 — see the module docstring above). ``Ctrl+L`` reaches the ask
    screen (lode-mkc.2's cited-Q&A screen -- the product's central bet -- left
    unreachable until lode-11io wired it up) the same way; it pops back to
    the previous screen on Escape, matching every sibling — and not the
    mnemonic ``Ctrl+A``, for the reason recorded at the binding below.
    Ctrl+Q quits immediately unless the current
    screen has unsaved state to confirm first (lode-0wj.8) — see
    :meth:`action_quit`.
    """

    TITLE = "lode"
    CSS_PATH = "lode.tcss"

    #: Registration convention for every E11 screen: name -> Screen subclass,
    #: pushed via ``push_screen("name")``. Extend this dict, not this class,
    #: when a new screen lands.
    SCREENS: ClassVar = {
        "capture": CaptureScreen,
        "config": ConfigScreen,
        "ask": AskScreen,
        "reconcile": ReconcileScreen,
        "browse": BrowseScreen,
        "tags": TagsScreen,
        "help": HelpScreen,
    }
    #: HelpScreen (lode-2bt3.2), like ReconcileScreen above, needs a
    #: constructed argument (the pre-push binding snapshot -- see
    #: action_show_help) -- pushed via a built instance, never the bare
    #: name; still registered here for discoverability, same convention.

    BINDINGS: ClassVar = [
        # Quit's footer entry is dropped (lode-2bt3.2, resolving the ticket's
        # own circular-dependency decision #10) to pay for the new Help
        # binding below without exceeding the 100-column footer budget:
        # ctrl+q is well known and Escape is an additional route out on most
        # screens, so hiding it from the footer costs little. The binding
        # itself stays fully live (priority=True, unchanged) -- only
        # show=False changes.
        #
        # ctrl+q is one of the most universally-known TUI conventions, Escape
        # covers the same ground on most screens, and every screen's help
        # overlay (Ctrl+_, below) lists it regardless.
        Binding("ctrl+q", "quit", "Quit", priority=True, show=False),
        # "Cfg" stays abbreviated: it is an App-level binding, so it renders
        # in every screen's footer, and BrowseScreen is the tightest
        # footer-bearing screen (gated by
        # tests/test_tui_footer_width_corpus.py) -- swapping in
        # "Config" fails BrowseScreen's "fits without hscroll" bar. Do not
        # restore "Config" without re-measuring BrowseScreen's footer, not
        # just EditScreen's -- an App-level label change is judged against
        # the tightest screen, and which screen that is can change ticket to
        # ticket.
        # These four use Textual's own builtin push_screen(name) action
        # string (App.action_push_screen) rather than a one-line hand-rolled
        # action_show_* wrapper -- that would be pure duplication of the
        # builtin.
        Binding("ctrl+o", "push_screen('config')", "Cfg"),
        Binding("ctrl+b", "push_screen('browse')", "Browse"),
        Binding("ctrl+t", "push_screen('tags')", "Tags"),
        # ctrl+l, not the mnemonic ctrl+a: TextArea/Input claim ctrl+a as a
        # builtin, so it would sit silently dead on every text-entry screen
        # (Capture/Ask/Edit). ctrl+l and ctrl+j were the last two
        # formally-safe letters after lode-juz8.1's rekey, and ctrl+j is the
        # raw LF byte -- so ctrl+l wins despite its own terminal-level
        # clear/redraw convention: the letter space is exhausted, not a first
        # choice. Full rationale and measurements: docs/keybindings.md. The
        # ctrl+a rule is enforced by tests/test_tui_ask_screen.py, not by this
        # comment -- a later ticket that re-binds it fails there.
        Binding("ctrl+l", "push_screen('ask')", "Ask"),
        # The keybinding help overlay (lode-2bt3.2). ``Ctrl+_`` -- Textual's
        # name for the 0x1f byte terminals emit for Ctrl+/ (the same
        # physical key as '?') -- rather than the unavailable ``Ctrl+?``
        # (terminals cannot encode ctrl+shift+/ as a distinct byte; verified
        # against textual 8.2.8's Keys vocabulary, which has no such entry).
        # Not a ctrl-letter, so it doesn't draw on the exhausted letter-space
        # this file's own "No formally-safe letter left" note above
        # documents; not a function key, so lode-juz8.1's ban stands
        # untouched. A REAL, visible App-level binding (not show=False) --
        # the overlay is a first-class, discoverable affordance, per the
        # ticket's own decision. Full rationale, the terminal-arrival
        # verification, and the footer-width measurement:
        # docs/keybindings.md.
        Binding("ctrl+underscore", "show_help", "Help"),
        # lode-av50: the two Kitty-keyboard-protocol names for the same
        # physical press. Textual negotiates that protocol by default (gated
        # only on TEXTUAL_DISABLE_KITTY_KEY, never set by lode itself), and
        # under it a terminal that reports no associated text sends the base
        # codepoint plus a modifier bitmask instead of the legacy 0x1f byte
        # the binding above depends on: CSI 45;6u -> "ctrl+shift+minus"
        # (Ctrl+_) and CSI 45;5u -> "ctrl+minus" (Ctrl+-, which the legacy
        # byte also covered). Both bound to the SAME action, alongside --
        # never replacing -- ctrl+underscore, which terminals off the
        # protocol, or reporting associated text, still rely on. Hidden --
        # one footer slot ("Help", above) already covers this action.
        # Full mechanism and empirical derivation: docs/keybindings.md.
        Binding("ctrl+shift+minus", "show_help", "Help", show=False),
        Binding("ctrl+minus", "show_help", "Help", show=False),
        # '?' is a convenience alias for the same action, reachable wherever
        # no TextArea/Input holds focus (freed by lode-2bt3.1). Hidden --
        # "Help" is already shown via the Ctrl+_ entry above; a duplicate
        # footer slot for the same action would waste the width lode-2bt3.2
        # dropping "Quit" just bought back.
        Binding("?", "show_help", "Help", show=False),
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
        # TUI theme config (lode-cwyk): note_body_theme is always set (falls
        # back to the existing NOTE_BODY_THEME singleton -- see
        # lode.theming's module docstring for the byte-identical-when-absent
        # contract); the app-chrome Theme is only registered/activated when
        # [tui.theme] is actually configured, so an absent section never
        # touches App's own "textual-dark" default at all.
        self.note_body_theme = resolve_note_body_theme(self.settings)
        if self.settings.tui.theme is not None:
            theme = resolve_theme(self.settings)
            self.register_theme(theme)
            self.theme = theme.name

    def on_mount(self) -> None:
        self.push_screen("capture")

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

    def action_show_help(self) -> None:
        """Ctrl+_ / '?': push the keybinding help overlay (lode-2bt3.2).

        Snapshots ``self.screen.active_bindings`` -- the active screen's own
        bindings merged with this app's, exactly what Textual's own
        ``BindingsTable`` would read -- **before** pushing
        :class:`~lode.tui.screens.help.HelpScreen`. Reading it any later
        (from inside the pushed modal) would see only the modal's own
        near-empty bindings instead, since ``Screen._modal_binding_chain``
        stops at the last modal on the stack. See that module's docstring
        for the full mechanism.
        """
        self.push_screen(HelpScreen(self.screen.active_bindings))


def run(*, db_path: Path | None = None, settings: Settings | None = None) -> str | None:
    """Entry point wired from ``lode tui`` — start the shell on the capture screen."""
    return LodeApp(db_path=db_path, settings=settings).run()

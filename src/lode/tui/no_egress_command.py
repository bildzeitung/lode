"""Command-palette no-egress toggle, shared by CaptureScreen and EditScreen (lode-pky9).

**Human decisions, 2026-09-08 (do not relitigate).** No new keybinding: with
no formally-safe ``ctrl+``-letter left on either screen
(``docs/keybindings.md``), the toggle lives in the command palette
(``ctrl+p``) instead -- infrequent enough that it need not be fast. Textual
8.2.8's ``Screen.COMMANDS`` is the mechanism: a screen lists a
:class:`~textual.command.Provider` subclass there and the palette
instantiates it fresh every time it opens, so the label below is read live
off the screen, not fixed at registration time.

One :class:`Provider` serves both screens rather than one apiece, because the
only thing that differs between them is how each computes "is it currently
withheld" and what "flip it" does (EditScreen writes through
:func:`~lode.tui.services.no_egress.toggle_note_no_egress` and confirms on
the clearing direction; CaptureScreen only flips an in-memory pending flag,
nothing to confirm). Both screens satisfy :class:`NoEgressCommandTarget`
instead.
"""

from __future__ import annotations

from typing import Protocol

from textual.command import DiscoveryHit, Hit, Hits, Provider


class NoEgressCommandTarget(Protocol):
    """What a screen must supply to host the no-egress palette command."""

    def no_egress_pending(self) -> bool:
        """Whether the note (or, on Capture, the pending flag) is currently withheld."""

    def no_egress_toggle(self) -> None:
        """Flip it. Owns its own confirm flow, if it needs one."""


def _command_text(pending: bool) -> str:
    """ "Mark no-egress" / "Clear no-egress" -- reflects the CURRENT state, so
    the palette entry itself doubles as a status readout (per the ticket's
    human decision)."""
    return "Clear no-egress" if pending else "Mark no-egress"


class NoEgressCommandProvider(Provider):
    """One command: toggle the active screen's no-egress flag.

    ``self.screen`` (from :class:`~textual.command.Provider`) is the screen
    that was active when the palette opened -- ``CaptureScreen`` and
    ``EditScreen`` both satisfy :class:`NoEgressCommandTarget`, so no
    ``isinstance`` branching is needed here.
    """

    async def discover(self) -> Hits:
        target: NoEgressCommandTarget = self.screen  # type: ignore[assignment]
        text = _command_text(target.no_egress_pending())
        yield DiscoveryHit(text, target.no_egress_toggle)

    async def search(self, query: str) -> Hits:
        target: NoEgressCommandTarget = self.screen  # type: ignore[assignment]
        text = _command_text(target.no_egress_pending())
        matcher = self.matcher(query)
        if (score := matcher.match(text)) > 0:
            yield Hit(score, matcher.highlight(text), target.no_egress_toggle)

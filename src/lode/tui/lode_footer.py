"""The one shared footer widget every E11 screen composes (lode-uczx).

Before this, each of the ten footer-bearing screens called Textual's stock
``Footer()`` directly, and two of them (``BrowseScreen`` lode-l38d.3,
``CaptureScreen`` lode-3rvw) additionally passed ``compact=True,
show_command_palette=False`` to fit their wider binding sets -- while the
other eight stayed bare. That is drift-by-default: a new screen that forgets
those two flags regresses silently, which is exactly how lode-3rvw's bug (the
app's own default/landing screen clipping) went unnoticed past lode-l38d.3.

**The fix is this ~4-line subclass, not a bigger custom widget.** The epic
that first raised "a shared custom footer widget" assumed it would be
materially larger than the stock-Footer-plus-flags approach; measured, it
isn't -- ``LodeFooter`` bakes the same two flags in once, byte-identical to
what ``BrowseScreen``/``CaptureScreen`` already passed explicitly (verified:
Capture's footer still consumes 77 columns with or without the swap). Every
one of the ten screens now composes ``LodeFooter()`` with no arguments; there
is no per-screen lever left to forget.

Rejected alternatives (do not revisit -- see ``docs/tui.md``'s footer
section for the fuller writeup):

- Repeat the two flags at each of the ten call sites: works, but is the
  drift-by-default this widget exists to close.
- Central CSS in ``lode.tcss`` targeting Textual's internal compact/palette
  classes (``.-compact``, ``.-command-palette``): at the 100-column minimum
  width (``docs/tui.md``) this genuinely fits too, but it depends on
  Textual-*internal* class names (the leading dash marks them as not public
  API), so a Textual upgrade could silently revert the look. This widget
  uses only ``Footer``'s public ``__init__`` parameters.

**The one seam for a future footer style change.** If the bracketed-key
style (``[d]elete``) or any other house look is ever revisited, this class
is the single place to make it -- not a re-audit of ten call sites. See
``docs/tui.md`` for why the bracketed style was ruled out.
"""

from __future__ import annotations

from textual.widgets import Footer


class LodeFooter(Footer):
    """lode's footer: the stock Footer with the house style baked in."""

    def __init__(self) -> None:
        super().__init__(compact=True, show_command_palette=False)

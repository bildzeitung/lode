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

**BOTH flags are load-bearing -- do not "simplify" either away.** This class
body is nothing but those two flags, which makes it the obvious target for a
future cleanup pass; it would be a regression. What each one does, and what
dropping it costs at the 100-column bound (``docs/tui.md``), measured:

- ``compact=True`` trims Textual's built-in ``FooterKey`` padding from 3
  columns of overhead per entry to 1 -- across Browse's 11 entries that
  alone is ~22 columns. Dropping it: Browse 110, Edit 110 (both clip).
- ``show_command_palette=False`` hides *only* the "^p palette" entry Textual
  auto-adds to every footer regardless of ``BINDINGS``. Dropping it: Browse
  100 with ``show_horizontal_scrollbar=True``, Edit 102 (both clip).

This is **not** the ``show=False`` binding-hiding that lode-l38d.3 ruled out:
``ctrl+p`` still opens the command palette with the flag off (verified) --
only the footer's own icon for it goes away, and the palette was never one of
lode's declared ``BINDINGS``. The guard is real rather than advisory: the
Browse and Edit footer tests' ``consumed <= 100`` asserts fail on either drop
(``tests/test_tui_browse_screen.py``).

**The one seam for a future footer style change.** If the bracketed-key
style (``[d]elete``) or any other house look is ever revisited, this class
is the single place to make it -- not a re-audit of ten call sites.

``docs/tui.md``'s footer section is the source of truth for the *decisions*
here -- why a shared widget beat central ``lode.tcss`` rules and repeated
per-call-site flags, and why the bracketed style was ruled out. Not restated
here: a second copy only drifts.
"""

from __future__ import annotations

from textual.widgets import Footer


class LodeFooter(Footer):
    """lode's footer: the stock Footer with the house style baked in."""

    def __init__(self) -> None:
        super().__init__(compact=True, show_command_palette=False)

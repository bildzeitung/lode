"""One module per lode TUI screen (E11).

Each screen is a self-contained :class:`textual.screen.Screen` subclass that
registers into :data:`lode.tui.app.LodeApp.SCREENS` — adding a new screen
(ask, passive connections, CAS-reconcile, config/diagnostics) is one new file
here plus one entry in that dict, never a change to an existing screen's
module. :mod:`lode.tui.screens.capture` is the first: instant capture, no AI
call anywhere in its save path (lode-mkc.1).
"""

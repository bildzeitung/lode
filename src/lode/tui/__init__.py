"""lode's Textual TUI (E11, ``docs/design.md`` §1: "The UI is a TUI precisely so
capture stays instant").

:mod:`lode.tui.app` holds the shared :class:`~lode.tui.app.LodeApp` shell every
E11 screen registers against; :mod:`lode.tui.screens` holds one module per
screen, kept self-contained so adding a screen never touches another's file.
"""

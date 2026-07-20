"""Custom Textual widgets shared across lode's screens, mirroring :mod:`lode.tui.screens`.

Each widget is a self-contained reusable :class:`textual.widget.Widget` subclass composed by
more than one screen — :class:`~lode.tui.widgets.lode_footer.LodeFooter` (every footer-bearing
screen) and :class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel`
(capture/edit). A small, one-off widget used by only its sole caller stays in that caller's
module instead (`docs/conventions.md`).
"""

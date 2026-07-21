"""``LodeStatic`` -- a ``Static`` that defaults to no markup parsing (lode-3dz2).

Same bug class, same seam rationale as ``LodeDataTable``
(:mod:`lode.tui.widgets.lode_data_table`): ``textual.widgets.Static`` defaults
``markup=True``, so a plain ``str`` fed to its constructor or ``.update()``
is parsed as Rich console markup and a literal ``[bracket]`` substring that
happens to look like a style tag is silently eaten. Precedent for pinning
``markup=False`` already exists per-widget
(:class:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel`,
lode-mkc.3) and per-site (``ask.py``'s results ``Static``, lode-ix4i); this
is the same fix promoted to a shared base class so every screen gets it for
free instead of remembering to pass the kwarg.

``markup`` stays a constructor kwarg (defaulted to ``False``, not
hardcoded) -- unlike ``RelatedNotesPanel``, a screen that genuinely wants
Rich markup parsed (a fixed, developer-authored string with an intentional
``[bold]`` tag, say) can still opt back in explicitly.
"""

from __future__ import annotations

from textual.visual import VisualType
from textual.widgets import Static


class LodeStatic(Static):
    """A ``Static`` defaulting to ``markup=False``.

    Drop-in replacement for ``textual.widgets.Static`` -- every screen under
    ``src/lode/tui/screens/`` constructs this instead (enforced by
    ``tests/test_tui_widget_seam_guard.py``).
    """

    def __init__(
        self,
        content: VisualType = "",
        *,
        expand: bool = False,
        shrink: bool = False,
        markup: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            content,
            expand=expand,
            shrink=shrink,
            markup=markup,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

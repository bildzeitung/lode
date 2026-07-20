"""Gate: no module under ``src/lode/tui/screens/`` imports bare ``DataTable``
or ``Static`` from ``textual.widgets`` (lode-3dz2).

THE DEFECT THIS CLOSES. A plain ``str`` cell/content fed to a stock
``textual.widgets.DataTable``/``Static`` is parsed as Rich console *markup* at
render time, silently eating a literal ``[bracket]`` substring that happens to
parse as a style tag (``"gh[pousr]_..."`` renders as ``"gh_..."``, no
exception). This has been fixed per-site four separate times (lode-7abi,
lode-ix4i x3) and a fifth live instance (``ConfigScreen``) turned up two files
away from the most recent pass -- a per-site sweep does not converge, because
every *new* screen starts undefended and the failure is silent.

THE SEAM. :class:`~lode.tui.widgets.lode_data_table.LodeDataTable` and
:class:`~lode.tui.widgets.lode_static.LodeStatic` close this once, at the one
place every cell/content value funnels through (``add_row``/``add_rows``/
``update_cell``/``update_cell_at`` for the table; the ``markup`` default for
``Static``). This test is the guard that keeps the seam actually closed: an
AST sweep over every module under ``src/lode/tui/screens/``, same shape as
``tests/test_deps_declared.py``'s import sweep, flagging any
``from textual.widgets import ...`` naming ``DataTable`` or ``Static``.

SCOPE: ``src/lode/tui/screens/`` only. The Lode widget subclasses themselves
(``src/lode/tui/widgets/lode_data_table.py``, ``lode_static.py``) necessarily
import the bare stock widgets to subclass them -- that module is the seam
itself, not a site the seam protects. ``src/lode/tui/widgets/`` is otherwise
unrestricted for the same reason ``related_notes_panel.py`` (predating this
guard) already gets to subclass ``Static`` directly: a widget *authoring* its
own subclass is exempt; a *screen consuming* a table/static is not.

NON-VACUITY (acceptance criterion): sabotaging the gate -- reintroducing a
bare ``from textual.widgets import DataTable`` (or ``Static``) into a real
screens/ module on disk -- must make it fail. Proven below rather than
asserted.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENS_DIR = REPO_ROOT / "src" / "lode" / "tui" / "screens"

_GUARDED_NAMES = frozenset({"DataTable", "Static"})


def _bare_widget_imports(path: Path) -> list[str]:
    """Every guarded name (``DataTable``/``Static``) this module imports
    directly from ``textual.widgets`` (not a submodule, not a relative import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "textual.widgets"
        ):
            for alias in node.names:
                if alias.name in _GUARDED_NAMES:
                    hits.append(alias.name)
    return hits


def _violations(screens_dir: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(screens_dir.rglob("*.py")):
        try:
            label = path.relative_to(REPO_ROOT)
        except ValueError:
            # The non-vacuity proof tests below sweep a pytest tmp_path, not
            # a real path under the repo -- fall back to the bare filename.
            label = path.name
        for name in _bare_widget_imports(path):
            violations.append(f"{label}: imports bare {name}")
    return violations


def test_no_screen_imports_bare_datatable_or_static():
    """Every screens/ module must use LodeDataTable/LodeStatic, not the stock
    textual.widgets DataTable/Static -- that's the whole seam (lode-3dz2)."""
    violations = _violations(SCREENS_DIR)
    assert not violations, (
        "src/lode/tui/screens/ must never import DataTable/Static directly "
        "from textual.widgets -- use lode.tui.widgets.lode_data_table."
        "LodeDataTable / lode.tui.widgets.lode_static.LodeStatic instead, so "
        "a bare str cell/content can never reach Rich's markup parser "
        "unguarded (lode-3dz2). Violations:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Non-vacuity proof: sabotaging the gate must make it fail.
# ---------------------------------------------------------------------------


def test_gate_detects_a_bare_datatable_import(tmp_path: Path) -> None:
    sabotaged = tmp_path / "sabotaged_screen.py"
    sabotaged.write_text("from textual.widgets import DataTable\n")
    violations = _violations(tmp_path)
    assert violations == ["sabotaged_screen.py: imports bare DataTable"]


def test_gate_detects_a_bare_static_import(tmp_path: Path) -> None:
    sabotaged = tmp_path / "sabotaged_screen.py"
    sabotaged.write_text("from textual.widgets import Header, Static\n")
    violations = _violations(tmp_path)
    assert violations == ["sabotaged_screen.py: imports bare Static"]


def test_gate_ignores_lode_widget_imports_and_other_textual_widgets(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean_screen.py"
    clean.write_text(
        "from textual.widgets import Header, Input\n"
        "from lode.tui.widgets.lode_data_table import LodeDataTable\n"
        "from lode.tui.widgets.lode_static import LodeStatic\n"
    )
    assert _violations(tmp_path) == []

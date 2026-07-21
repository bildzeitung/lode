"""Gate: docs/keybindings.md's Screen-level table must match real Screen
BINDINGS (lode-d79b).

THE DEFECT THIS CLOSES. docs/keybindings.md's Screen-level table
(`## Current keymap` > `### Screen-level`) is a hand-maintained mirror of
every Screen subclass's `BINDINGS` -- nothing forced it to follow the code,
so when lode-olmi.2 retired `NoteViewScreen` the table kept two dead rows
until a human noticed (lode-s4z5). This is the mechanized guard: import every
Screen subclass under `src/lode/tui/screens/`, read its own `BINDINGS`, parse
the doc's table, and assert the two sides agree on which (screen, key) pairs
exist.

WHAT "action" MEANS HERE (design decision, see lode-d79b's `--design`).
The ticket's acceptance criteria describe the assertion as "(screen, key,
action)" while explicitly excluding the description/footer label ("Rel"/
"Hist" -- EditScreen's footer-width-abbreviated labels) as churn-prone.
Investigated and rejected: matching the doc's free-form 'Action' column
prose (e.g. "Show version history") against either candidate source field --
the raw action identifier ("show_history") or the footer label ("Hist") --
neither matches consistently; the doc's Action column is elaborated human
prose, not a machine identifier, and rewriting it into terse tokens would
mean rewording carefully-written documentation prose well beyond "the table"
this ticket scopes itself to. So: this gate asserts set-equality on
(screen, key) pairs only. Both of the ticket's concrete, unambiguous failure
modes -- "a documented screen no longer exists" and "a real binding is
undocumented" -- are fully captured by that alone; a discovered Binding
always carries *some* action, so "action" in the sense of "a real, live
feature is bound here" is satisfied structurally, without literal text
matching. Verified this passes on day one with zero doc changes.

SCOPE (deliberate, per the ticket): the Screen-level table only. The doc's
prose sections (traps, rationale, letter-space accounting) and its separate
App-level table are out of scope -- they either can't be mechanized or
weren't asked for here.

PARSING NOTES / TRAPS:

* The table forward-fills blank Screen/File cells across a screen's
  multi-key rows (see any screen with >1 binding, e.g. `EditScreen`) -- the
  parser must carry the last-seen Screen/File value down through blank
  cells, not treat them as a new (unnamed) screen.
* `RelatedNotesPanel` has its own row in the same table, annotated
  "(focusable widget, not a `Screen`)" -- it must never be compared against
  Screen subclasses. Its File cell is `related_notes_panel.py`, with no
  `screens/` prefix (every genuine Screen row's File cell reads
  `screens/<module>.py`), which is the mechanical signal this parser uses to
  exclude it.
* A screen module often imports *other* Screen subclasses (to push them) --
  `cls.__module__ == module.__name__` is what restricts discovery to the
  class actually *defined* in that file, per the "one Screen per module"
  convention (docs/conventions.md).

NON-VACUITY (acceptance criterion): sabotaging the gate -- dropping a real
binding from the comparison, or documenting a binding that doesn't exist --
must make it fail. Both are proven below rather than asserted.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

from textual.screen import Screen

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENS_DIR = REPO_ROOT / "src" / "lode" / "tui" / "screens"
KEYBINDINGS_DOC = REPO_ROOT / "docs" / "keybindings.md"

_BACKTICKED = re.compile(r"`([^`]+)`")


# ---------------------------------------------------------------------------
# Source side: discover every Screen subclass under src/lode/tui/screens/ and
# read its own BINDINGS.
# ---------------------------------------------------------------------------


def discover_screen_classes() -> dict[str, type]:
    """Every Screen (or ModalScreen) subclass *defined in* (not merely
    imported into) a module under src/lode/tui/screens/, keyed by class
    name. Skips `__init__.py` and private helper modules (`_`-prefixed --
    e.g. `_link_open.py`), which hold no Screen subclass of their own."""
    classes: dict[str, type] = {}
    for path in sorted(SCREENS_DIR.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        module = importlib.import_module(f"lode.tui.screens.{path.stem}")
        for obj in vars(module).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, Screen)
                and obj is not Screen
                and obj.__module__ == module.__name__
            ):
                classes[obj.__name__] = obj
    return classes


def source_screen_key_pairs(classes: dict[str, type]) -> set[tuple[str, str]]:
    """(screen class name, key) for every entry in each class's *own*
    BINDINGS (cls.__dict__, not an inherited attribute)."""
    pairs: set[tuple[str, str]] = set()
    for name, cls in classes.items():
        for binding in cls.__dict__.get("BINDINGS", []):
            # Every screen's BINDINGS use Textual `Binding` objects (the repo's
            # uniform style); a bare-tuple entry would (correctly) fail loud here.
            pairs.add((name, binding.key))
    return pairs


# ---------------------------------------------------------------------------
# Doc side: parse the Screen-level markdown table.
# ---------------------------------------------------------------------------


def _screen_level_table_lines(text: str) -> list[str]:
    """The raw `|`-prefixed lines of the table directly under the
    '### Screen-level' heading, stopping at the first line after the table
    that isn't itself a table row."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "### Screen-level":
            start = i + 1
            break
    assert start is not None, (
        "docs/keybindings.md has no '### Screen-level' heading -- "
        "has the doc been restructured?"
    )
    table_lines: list[str] = []
    in_table = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            table_lines.append(stripped)
        elif in_table:
            break
    return table_lines


def _parse_row(line: str) -> list[str]:
    cells = line.split("|")
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _first_backticked(cell: str) -> str | None:
    match = _BACKTICKED.search(cell)
    return match.group(1) if match else None


def parse_doc_screen_key_pairs(doc_path: Path) -> set[tuple[str, str]]:
    """(screen class name, key) for every row of the Screen-level table
    whose File cell points under `screens/` -- i.e. every row that
    documents a real Screen subclass, excluding e.g. RelatedNotesPanel's
    row (a focusable widget, not a Screen; its File cell has no `screens/`
    prefix)."""
    text = doc_path.read_text(encoding="utf-8")
    rows = [_parse_row(line) for line in _screen_level_table_lines(text)]
    data_rows = rows[2:]  # rows[0] = header, rows[1] = the --- separator

    pairs: set[tuple[str, str]] = set()
    current_screen: str | None = None
    current_file: str | None = None
    for cells in data_rows:
        screen_cell, file_cell, key_cell = cells[0], cells[1], cells[2]
        if screen_cell:
            current_screen = _first_backticked(screen_cell)
        if file_cell:
            current_file = _first_backticked(file_cell)
        if current_file is None or not current_file.startswith("screens/"):
            continue  # e.g. RelatedNotesPanel -- a widget, not a Screen
        key = _first_backticked(key_cell)
        assert current_screen is not None and key is not None, (
            f"Malformed Screen-level table row (no backticked screen/key): {cells!r}"
        )
        pairs.add((current_screen, key))
    return pairs


# ---------------------------------------------------------------------------
# The gate itself.
# ---------------------------------------------------------------------------


def test_screen_bindings_match_docs_table():
    """Every real Screen binding must have a matching row in
    docs/keybindings.md's Screen-level table, and vice versa."""
    source_pairs = source_screen_key_pairs(discover_screen_classes())
    doc_pairs = parse_doc_screen_key_pairs(KEYBINDINGS_DOC)

    undocumented = source_pairs - doc_pairs
    stale = doc_pairs - source_pairs

    assert not undocumented, (
        f"Screen binding(s) exist in code but have no row in "
        f"docs/keybindings.md's Screen-level table: {sorted(undocumented)}. "
        f"Add a row for each."
    )
    assert not stale, (
        f"docs/keybindings.md's Screen-level table documents (screen, key) "
        f"pairs that no longer exist in code: {sorted(stale)}. Either the "
        f"screen was retired or the binding was removed/rekeyed -- update "
        f"the table (see lode-s4z5 for precedent)."
    )


# ---------------------------------------------------------------------------
# Non-vacuity proof (acceptance criterion): sabotaging the gate must make it
# fail, in both directions.
# ---------------------------------------------------------------------------


def test_gate_is_non_vacuous_removing_a_real_binding_from_docs_is_flagged():
    """Simulate a doc that forgot EditScreen's ctrl+g row -- the gate must
    flag it as undocumented."""
    source_pairs = source_screen_key_pairs(discover_screen_classes())
    doc_pairs = parse_doc_screen_key_pairs(KEYBINDINGS_DOC)
    assert ("EditScreen", "ctrl+g") in doc_pairs  # precondition

    sabotaged_doc_pairs = doc_pairs - {("EditScreen", "ctrl+g")}
    undocumented = source_pairs - sabotaged_doc_pairs
    assert ("EditScreen", "ctrl+g") in undocumented


def test_gate_is_non_vacuous_stale_doc_row_is_flagged():
    """Simulate a doc that still documents a retired screen (the exact
    historical defect, lode-s4z5's NoteViewScreen/h) -- the gate must flag
    it as stale."""
    source_pairs = source_screen_key_pairs(discover_screen_classes())
    doc_pairs = parse_doc_screen_key_pairs(KEYBINDINGS_DOC)

    sabotaged_doc_pairs = doc_pairs | {("NoteViewScreen", "h")}
    stale = sabotaged_doc_pairs - source_pairs
    assert ("NoteViewScreen", "h") in stale


# ---------------------------------------------------------------------------
# Trap regression tests: each design trap the parser/discovery had to
# handle, pinned directly against the helpers above.
# ---------------------------------------------------------------------------


def test_related_notes_panel_row_is_excluded_not_a_screen():
    """RelatedNotesPanel is a focusable widget, not a Screen -- its doc row
    must never surface in the parsed pairs, or the gate would spuriously
    demand a RelatedNotesPanel Screen class that doesn't exist."""
    doc_pairs = parse_doc_screen_key_pairs(KEYBINDINGS_DOC)
    assert not any(screen == "RelatedNotesPanel" for screen, _key in doc_pairs)


def test_forward_fill_carries_screen_across_blank_rows():
    """A screen with more than one binding (e.g. EditScreen) leaves its
    Screen/File cells blank on every row after the first -- both rows must
    still resolve to the same screen name, not an unnamed one."""
    doc_pairs = parse_doc_screen_key_pairs(KEYBINDINGS_DOC)
    assert ("EditScreen", "ctrl+s") in doc_pairs
    assert ("EditScreen", "ctrl+n") in doc_pairs  # a later, forward-filled row


def test_discovery_and_parsing_are_non_trivially_populated():
    """Guards against the gate going vacuously green if screen discovery or
    doc parsing silently returns nothing (e.g. a swallowed import error, or
    a renamed heading) -- both sets would then be equally empty and the
    equality assertion would pass for the wrong reason."""
    classes = discover_screen_classes()
    doc_pairs = parse_doc_screen_key_pairs(KEYBINDINGS_DOC)
    assert len(classes) >= 10
    assert len(doc_pairs) >= 10


def test_only_classes_defined_in_their_own_module_are_discovered():
    """edit.py imports several other screens (to push them) -- only
    EditScreen, the class it actually defines, may be attributed to it."""
    classes = discover_screen_classes()
    assert classes["EditScreen"].__module__ == "lode.tui.screens.edit"
    # Screens edit.py merely imports must be attributed to their own module,
    # not counted twice / misattributed to edit.py.
    assert classes["ReconcileScreen"].__module__ == "lode.tui.screens.reconcile"

"""Gate: every top-level third-party import under src/ must be declared in
pyproject.toml (lode-mrdu).

THE DEFECT THIS CLOSES. The same defect class was hand-found three times,
each time incidentally, during review of an unrelated ticket:

1. rich            (lode-l38d.1)  -- transitive via textual+typer, promoted
   to a direct dependency (docs/stack.md:13).
2. huggingface_hub (lode-l38d.6)  -- transitive via fastembed, promoted.
3. pyarrow         (lode-zcoz)    -- transitive via lancedb, promoted.

Each time, ``src/`` imported a package directly while pyproject.toml relied
on someone else's dependency graph to provide it -- silently correct until
whichever transitive provider ever drops it, and never caught by nox -t fix
or nox -s tests. This test is the gate: an AST sweep over every top-level
import in ``src/``, mapped to its installed distribution via
``importlib.metadata.packages_distributions()``, compared against
pyproject.toml's ``[project].dependencies`` + every ``optional-dependencies``
extra (the dev extra included, so ``pytest``/``nox``/etc. count as declared).

SCOPE (deliberate, per the ticket): ``src/`` only, not ``tests/`` -- test
imports are dev-extra-only and noisier, and ``src/`` is where the shipped
defect actually lives.

DESIGN NOTES / TRAPS (see the ticket description for the full derivation):

* Import name != distribution name -- ``packages_distributions()`` is the
  correct mapping, and it needs the package INSTALLED. A dep that is
  declared but not installed resolves to no distribution at all, and that
  case is NOT collapsed into "declared" -- see UNDECLARED vs. UNRESOLVED
  below.
* PEP 503 normalisation (case + ``-``/``_``/``.`` all collapsed to ``-``)
  before comparing distribution names against pyproject.toml requirement
  strings.
* Requirement strings are stripped of markers (``; python_version < ...``),
  extras (``foo[bar]``), and version specifiers before comparing.
* ``ast.ImportFrom`` with ``level > 0`` is a relative import
  (``from . import x``, ``from ..pkg import y``) and is never external --
  excluding it is load-bearing (a bare ``from . import`` must never register
  as a third-party top).

NON-VACUITY (acceptance criterion): sabotaging the gate -- removing a real
declared dependency from the comparison set, or introducing an import that
resolves to a real but undeclared distribution -- must make it fail. Both are
proven below rather than asserted; a gate that cannot fail is not a gate.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_STDLIB_AND_FIRST_PARTY = frozenset(sys.stdlib_module_names) | {"lode"}


def _top_level_imports(path: Path) -> set[str]:
    """Every top-level module name a single file imports.

    ``ast.Import`` covers ``import foo.bar`` (top = ``foo``).
    ``ast.ImportFrom`` covers ``from foo.bar import baz`` (top = ``foo``) --
    but only when ``level == 0``: a nonzero level is a relative import
    (``from . import x`` / ``from ..pkg import y``), never external, and
    must never be added here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tops.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                tops.add(node.module.split(".", 1)[0])
    return tops


def _third_party_tops(src_dir: Path) -> set[str]:
    """Top-level import names under `src_dir`, minus stdlib and first-party."""
    tops: set[str] = set()
    for path in src_dir.rglob("*.py"):
        tops |= _top_level_imports(path)
    return tops - _STDLIB_AND_FIRST_PARTY


def _normalise(name: str) -> str:
    """PEP 503: lowercase, any run of ``-``/``_``/``.`` collapsed to one ``-``."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirement_name(requirement: str) -> str:
    """Strip a PEP 508 requirement string down to its bare project name.

    Drops the environment marker (after ``;``), the extras group
    (``foo[bar]``), and any version specifier, in that order.
    """
    requirement = requirement.split(";", 1)[0]
    requirement = requirement.split("[", 1)[0]
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return match.group(1) if match else requirement.strip()


def _declared_distributions(pyproject_path: Path) -> set[str]:
    """Every distribution pyproject.toml declares: dependencies + every
    optional-dependencies extra (the dev extra included -- a test-only
    import like pytest/nox must count as declared, not undeclared)."""
    data = tomllib.loads(pyproject_path.read_text())
    project = data["project"]
    requirements = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirements.extend(extra_requirements)
    return {_normalise(_requirement_name(r)) for r in requirements}


def _classify(
    third_party_tops: set[str], declared: set[str]
) -> tuple[list[str], list[str]]:
    """Split third-party top-level import names into (undeclared, unresolved).

    UNDECLARED: the import resolves to an installed distribution, but that
    distribution is not in `declared`.
    UNRESOLVED: the import maps to NO installed distribution at all. This is
    kept distinct from UNDECLARED -- and distinct from "fine" -- on purpose:
    an uninstalled dependency must never silently read as declared just
    because it also fails to register as undeclared.
    """
    dist_map = packages_distributions()
    undeclared: list[str] = []
    unresolved: list[str] = []
    for top in sorted(third_party_tops):
        dists = dist_map.get(top)
        if not dists:
            unresolved.append(top)
            continue
        normalised_dists = {_normalise(d) for d in dists}
        if not normalised_dists & declared:
            undeclared.append(top)
    return undeclared, unresolved


# ---------------------------------------------------------------------------
# The gate itself.
# ---------------------------------------------------------------------------


def test_all_src_imports_are_declared():
    """Every third-party top-level import under src/ must be declared in
    pyproject.toml, and must actually be installed (not merely declared)."""
    third_party = _third_party_tops(SRC_DIR)
    declared = _declared_distributions(PYPROJECT)
    undeclared, unresolved = _classify(third_party, declared)

    assert not undeclared, (
        f"src/ imports a distribution not declared in pyproject.toml's "
        f"[project].dependencies (or an optional-dependencies extra): "
        f"{undeclared}. Declare it (see docs/stack.md for precedent)."
    )
    assert not unresolved, (
        f"src/ imports a name that resolves to no installed distribution at "
        f"all: {unresolved}. This is not the same as 'declared' -- install "
        f"the real package (or fix a typo'd import) before trusting this gate."
    )


# ---------------------------------------------------------------------------
# Non-vacuity proof (acceptance criterion): sabotaging the gate must make it
# fail. Both sabotage directions from the ticket are proven directly against
# the gate's own helpers, using the real src/ tree and pyproject.toml.
# ---------------------------------------------------------------------------


def test_gate_is_non_vacuous_removing_a_declared_dep_fails_it():
    """Removing pyarrow's declaration from the comparison set must surface
    it as UNDECLARED -- src/lode/vectorstore.py genuinely imports it
    (lode-zcoz)."""
    third_party = _third_party_tops(SRC_DIR)
    declared = _declared_distributions(PYPROJECT)
    assert "pyarrow" in declared  # precondition: it really is declared today

    sabotaged_declared = declared - {"pyarrow"}
    undeclared, _unresolved = _classify(third_party, sabotaged_declared)
    assert "pyarrow" in undeclared


def test_gate_is_non_vacuous_undeclared_installed_import_is_flagged(tmp_path):
    """A top-level import that resolves to a real, installed distribution
    which pyproject.toml never declares must be reported UNDECLARED. `click`
    is installed transitively (via typer) but is not declared directly --
    exactly the defect shape this gate exists to catch."""
    sneaky_module = tmp_path / "sneaky.py"
    sneaky_module.write_text("import click\n")

    third_party = _top_level_imports(sneaky_module) - _STDLIB_AND_FIRST_PARTY
    declared = _declared_distributions(PYPROJECT)
    assert "click" not in declared  # precondition: genuinely undeclared today

    undeclared, unresolved = _classify(third_party, declared)
    assert undeclared == ["click"]
    assert unresolved == []


# ---------------------------------------------------------------------------
# Trap regression tests (acceptance criteria): each design trap the
# prototype hit, pinned directly against the helpers above.
# ---------------------------------------------------------------------------


def test_uninstalled_import_is_unresolved_not_undeclared(tmp_path):
    """An import that maps to no installed distribution at all is
    UNRESOLVED, never collapsed into UNDECLARED or silently treated as
    fine."""
    sneaky_module = tmp_path / "sneaky.py"
    sneaky_module.write_text("import totally_nonexistent_package_xyz\n")

    third_party = _top_level_imports(sneaky_module)
    declared = _declared_distributions(PYPROJECT)
    undeclared, unresolved = _classify(third_party, declared)

    assert unresolved == ["totally_nonexistent_package_xyz"]
    assert undeclared == []


def test_relative_imports_are_excluded(tmp_path):
    """ast.ImportFrom with level > 0 is a relative import and must never
    register as a top-level (potentially third-party) import, regardless of
    what module name follows the dots."""
    relative_module = tmp_path / "relative.py"
    relative_module.write_text(
        "from . import sibling\n"
        "from .. import cousin\n"
        "from .pkg import thing\n"
        "from ..pkg.sub import other\n"
    )
    assert _top_level_imports(relative_module) == set()


def test_stdlib_imports_are_excluded():
    """Stdlib modules must never appear in the third-party set."""
    third_party = _third_party_tops(SRC_DIR)
    assert not (third_party & {"os", "sys", "pathlib", "json", "re", "typing"})


def test_dev_extra_is_included_in_declared():
    """The dev extra must be included in `declared`, else a test-only import
    (pytest, nox) would read as undeclared."""
    declared = _declared_distributions(PYPROJECT)
    assert "pytest" in declared
    assert "nox" in declared


def test_requirement_name_strips_extras_markers_and_specifiers():
    """Pin the requirement-string stripping the gate depends on: markers,
    extras, and version specifiers must not leak into the compared name."""
    assert _requirement_name("foo") == "foo"
    assert _requirement_name("foo>=1.0") == "foo"
    assert _requirement_name("foo[extra]>=1.0") == "foo"
    assert _requirement_name("foo; python_version < '3.12'") == "foo"
    assert _requirement_name("foo[extra]; python_version < '3.12'") == "foo"


def test_normalise_applies_pep503_collapsing():
    """PEP 503: case-insensitive, runs of -/_/. all equivalent."""
    assert _normalise("Huggingface_Hub") == _normalise("huggingface-hub")
    assert _normalise("foo.bar") == _normalise("foo-bar") == _normalise("foo_bar")

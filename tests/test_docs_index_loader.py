"""Pins the choice (c) semantics ``lode-7l68`` chose for the bootstrap each
``scripts/docs_index_*.py`` caller uses to reach scripts/docs_index_loader.py:
uncached, registering no public ``sys.modules`` name, while ``load_sibling()``'s
own caching still holds. Rationale, rejected alternatives, and the
stateless-loader invariant this rests on: the ``lode-7l68`` entry in
docs/decisions.md.

These assert against the SHIPPED callers and the shipped loader, never against a
copy of the bootstrap defined here -- a test that re-implements the dance would
stay green through exactly the regression it exists to catch.
"""

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = REPO_ROOT / "scripts"
_LOADER_PATH = _SCRIPTS / "docs_index_loader.py"

#: The three callers that each carry their own bootstrap (the ticket's choice
#: (c): three short uncached ones, not one shared one).
_CALLERS = (
    "docs_index_build.py",
    "docs_index_log.py",
    "docs_index_query.py",
)


def _load_uncached(path: Path, name: str) -> ModuleType:
    """Load ``path`` without registering ``name`` in ``sys.modules``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_caller_registers_a_sys_modules_name_for_its_loader_bootstrap() -> None:
    """The regression pin: re-adding ``sys.modules[...] = loader`` to any
    caller's bootstrap must turn this red. Scanned in the source rather than
    observed at runtime, because the failure is a *write* that a passing import
    would leave behind only on the first load of the session."""
    for filename in _CALLERS:
        source = (_SCRIPTS / filename).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                assert not (
                    isinstance(target.value, ast.Attribute)
                    and target.value.attr == "modules"
                ), (
                    f"scripts/{filename} line {node.lineno} writes to sys.modules; "
                    "each caller's docs_index_loader.py bootstrap must stay "
                    "uncached (lode-7l68) so it needs no private cache name"
                )


def test_loading_a_caller_leaves_no_public_loader_name_in_sys_modules() -> None:
    """The public name ``docs_index_loader`` is what candidates (a) and (b)
    would have registered, and what this choice avoids needing at all -- so
    conftest.load_module_from_path's 'name not already in sys.modules' assert
    can never be tripped by it."""
    sys.modules.pop("docs_index_loader", None)
    for filename in _CALLERS:
        _load_uncached(_SCRIPTS / filename, f"_pin_lode_7l68_{filename[:-3]}")
        assert "docs_index_loader" not in sys.modules


def test_loader_module_is_stateless() -> None:
    """The invariant choice (c) rests on: N independent loads of
    docs_index_loader.py are harmless only while it holds no top-level state.
    A dataclass/enum or a module-level value would make a second load create a
    DISTINCT class object and break ``isinstance`` silently."""
    tree = ast.parse(_LOADER_PATH.read_text())
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the module docstring
        raise AssertionError(
            f"scripts/docs_index_loader.py line {node.lineno} adds top-level "
            f"{type(node).__name__} state; the uncached bootstrap in each caller "
            "(lode-7l68) is only sound while this module stays stateless"
        )


def test_downstream_sibling_caching_survives_independent_loader_copies() -> None:
    """Even loaded twice, independently, ``load_sibling()``'s own caching (a
    ``sys.modules`` lookup, not loader-instance state) returns the SAME
    downstream module object for one private name -- the guarantee the three
    callers and their tests depend on."""
    private_name = "_test_docs_index_loader_pin_chunker"
    sys.modules.pop(private_name, None)
    try:
        first = _load_uncached(_LOADER_PATH, "_pin_lode_7l68_loader_a")
        second = _load_uncached(_LOADER_PATH, "_pin_lode_7l68_loader_b")
        assert first is not second
        assert first.load_sibling(
            private_name, "docs_index_chunker.py"
        ) is second.load_sibling(private_name, "docs_index_chunker.py")
    finally:
        sys.modules.pop(private_name, None)

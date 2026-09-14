"""Pins the choice (c) semantics lode-7l68 made for scripts/docs_index_loader.py's
OWN bootstrap -- distinct from the caching load_sibling() itself does for the
modules it loads.

lode-wtk2 consolidated the sibling-loader BODY into docs_index_loader.py's
load_sibling(), but each of docs_index_build.py, docs_index_log.py, and
docs_index_query.py still needs to load docs_index_loader.py itself first,
via the same spec_from_file_location/exec_module dance the loader exists to
remove one level up. lode-7l68 chose to leave that one, innermost bootstrap
UNCACHED: docs_index_loader.py defines only load_sibling(), no top-level
state, so a second independent load of it is harmless, and skipping the
sys.modules registration means the bootstrap never needs a private cache
name of its own -- so it can never collide with a test's own private name
for docs_index_build/docs_index_log/docs_index_query (the actual hazard the
ticket's constraint warns about; see conftest.load_module_from_path's own
assert).

This is a small, deliberate design choice among three non-free candidates
(see the ticket), so it is pinned here rather than left to be silently
undone by a future "helpful" refactor that re-adds caching.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LOADER_PATH = REPO_ROOT / "scripts" / "docs_index_loader.py"


def _load_loader_uncached() -> object:
    """Mirror each caller's own bootstrap exactly: no sys.modules lookup or
    registration, a fresh load every call."""
    spec = importlib.util.spec_from_file_location("docs_index_loader", _LOADER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repeated_loads_are_independent_module_objects() -> None:
    """Two independent bootstraps (as docs_index_build.py and
    docs_index_query.py each run their own) must NOT be the same object --
    that is the whole point of leaving this one bootstrap uncached."""
    first = _load_loader_uncached()
    second = _load_loader_uncached()
    assert first is not second
    assert first.load_sibling is not second.load_sibling


def test_uncached_loads_never_register_a_public_sys_modules_name() -> None:
    """The bootstrap must never leave a 'docs_index_loader' entry in
    sys.modules -- that public name is exactly what candidates (a) and (b)
    would have registered, and what this choice avoids needing at all."""
    sys.modules.pop("docs_index_loader", None)
    _load_loader_uncached()
    assert "docs_index_loader" not in sys.modules


def test_downstream_sibling_caching_still_works_across_independent_loader_copies() -> (
    None
):
    """Even though the loader module itself is loaded twice, independently,
    load_sibling()'s OWN caching (a module-global sys.modules lookup, not
    loader-instance state) still returns the SAME downstream module object
    for the same private name -- which is the guarantee the three real
    callers, and their tests, depend on."""
    private_name = "_test_docs_index_loader_pin_chunker"
    sys.modules.pop(private_name, None)
    try:
        loader_copy_1 = _load_loader_uncached()
        loader_copy_2 = _load_loader_uncached()
        assert loader_copy_1 is not loader_copy_2

        chunker_via_1 = loader_copy_1.load_sibling(
            private_name, "docs_index_chunker.py"
        )
        chunker_via_2 = loader_copy_2.load_sibling(
            private_name, "docs_index_chunker.py"
        )
        assert chunker_via_1 is chunker_via_2
    finally:
        sys.modules.pop(private_name, None)

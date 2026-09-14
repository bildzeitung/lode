"""Shared ``scripts/`` sibling-module loader (``lode-wtk2``).

``scripts/`` is not an installed package, so a plain ``import`` of one
``scripts/*.py`` file from another fails for any caller that does not have
``scripts/`` on ``sys.path`` -- which includes every test in this repo,
loaded by path via ``tests/conftest.py``'s ``load_module_from_path``. Both
``docs_index_build.py`` and ``docs_index_query.py`` used to carry their own,
byte-for-byte-identical copy of this loader; this module is the one
implementation both call.

Loading under a name PRIVATE to the caller (never a name any test or CLI
chooses) is what avoids a collision with any other loader of the same file,
and the ``sys.modules`` cache is what keeps one private name mapped to
exactly one module object -- callers that then pull classes/functions off
the returned module (e.g. ``Unit``, ``chunk_corpus``) depend on getting the
SAME object back on every call, not a fresh, independent load (a second
independent load of the same file creates a distinct class object and
breaks ``isinstance`` silently).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_sibling(name: str, filename: str) -> ModuleType:
    """Load a ``scripts/`` sibling module under a PRIVATE ``sys.modules`` name.

    ``filename`` is resolved relative to THIS module's own directory
    (``scripts/``), not the caller's -- every caller of this function lives
    in ``scripts/`` too, so this is always correct and never needs a
    caller-supplied base path.
    """
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

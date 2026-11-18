"""Startup import boundary: the CLI and TUI entry modules must not load heavy stacks.

``lode add`` is instant by design (``docs/design.md``), so importing ``lode.cli`` or
``lode.tui.app`` must not pull in trafilatura (web-fetch extraction), pyarrow (the vector
store schema) or networkx (the retrieval graph). trafilatura and pyarrow are imported
inside the function that uses them; networkx is a module-scope import of
``lode.retrieval``, so it is ``lode.retrieval`` that callers import inside a function
(lode-g2ko; ``docs/decisions.md``).

Runs in a **subprocess**: a fresh interpreter is the real-world condition, and in-process
these modules are already resident from other tests, which would make the assertion
vacuous (same reasoning as ``tests/test_auth.py``'s SDK guard).
"""

import subprocess
import sys

import pytest

HEAVY = ("trafilatura", "pyarrow", "networkx")


@pytest.mark.parametrize("module", ["lode.cli", "lode.tui.app"])
def test_startup_import_leaves_heavy_stacks_unloaded(module: str) -> None:
    code = f"import {module}, sys; print([m for m in {HEAVY!r} if m in sys.modules])"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert proc.stdout.strip() == "[]", (
        f"importing {module} loaded {proc.stdout.strip()} -- keep those imports (for "
        "networkx, the `lode.retrieval` import) inside the function that uses them "
        "(lode-g2ko)"
    )

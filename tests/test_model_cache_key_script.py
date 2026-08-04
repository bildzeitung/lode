"""Tests for scripts/model-cache-key.py, the CI cache-key derivation (lode-sx23).

Both ``.github/workflows/tests.yml`` and ``.github/workflows/coverage.yml``
call this script (as ``python scripts/model-cache-key.py >> "$GITHUB_OUTPUT"``)
instead of each keeping its own copy of the derivation -- this is the guard
that the ONE implementation actually produces the expected ``key=...`` line
against lode's real pinned ``Settings``, and stays byte-identical to what a
second invocation produces (the whole point of extracting it: both workflows
must derive the same key from the same source).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package, so load by file path via the shared
# helper (tests/conftest.py). The filename has hyphens, so it could never be
# imported as `model-cache-key` -- the loaded module gets a valid name here.
model_cache_key = load_module_from_path(
    "model_cache_key", REPO_ROOT / "scripts" / "model-cache-key.py"
)


def test_cache_key_matches_pinned_settings() -> None:
    from lode.config import Settings

    s = Settings()
    ids = f"{s.embedding_model}-{s.rerank_model}-{s.entailment_model}"
    expected = "models-" + ids.replace("/", "_")
    assert model_cache_key.cache_key() == expected


def test_cache_key_is_stable_across_calls() -> None:
    # Both workflows invoke the script independently -- the derivation must
    # be a pure function of the pinned Settings defaults, not something that
    # could drift between two calls (e.g. via ordering or environment).
    assert model_cache_key.cache_key() == model_cache_key.cache_key()


def test_script_emits_github_output_line() -> None:
    # End-to-end: running the script as the workflows do must print exactly
    # one `key=...` line, ready for direct redirection into $GITHUB_OUTPUT.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "model-cache-key.py")],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("key=models-")
    assert lines[0] == f"key={model_cache_key.cache_key()}"

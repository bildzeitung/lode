"""uv.lock must carry a sha256 for every registry artifact (lode-s09w.4).

``uv sync`` / ``uv run --frozen`` verify each downloaded artifact against the hash
recorded in ``uv.lock`` and refuse a mismatch (measured on uv 0.11.7; see
docs/stack.md, "Dependency locking"). That verification is only as strong as the
lock's coverage, so this pins the offline half: no registry-sourced artifact may
appear in ``uv.lock`` without a ``sha256`` hash.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _unhashed_registry_artifacts(lock: dict) -> list[str]:
    bad = []
    for pkg in lock["package"]:
        if "registry" not in pkg.get("source", {}):
            continue
        artifacts = list(pkg.get("wheels", []))
        if "sdist" in pkg:
            artifacts.append(pkg["sdist"])
        if not artifacts:
            bad.append(f"{pkg['name']}: no artifacts")
        bad.extend(
            f"{pkg['name']}: {a.get('url', '?')}"
            for a in artifacts
            if not _SHA256.match(a.get("hash", ""))
        )
    return bad


def test_every_registry_artifact_in_uv_lock_has_a_sha256() -> None:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert any("registry" in p.get("source", {}) for p in lock["package"])
    assert _unhashed_registry_artifacts(lock) == []


def test_the_check_catches_a_missing_hash() -> None:
    lock = {
        "package": [
            {
                "name": "x",
                "source": {"registry": "https://pypi.org/simple"},
                "wheels": [{"url": "u", "hash": "sha256:" + "0" * 64}, {"url": "v"}],
            }
        ]
    }
    assert _unhashed_registry_artifacts(lock) == ["x: v"]

"""Drift gate for the docs-site's derived reference pages (lode-fhql.15).

`scripts/generate_derived_docs.py` derives `docs/keymap.md` from `docs/keybindings.md`'s "Current
keymap" tables, and `docs/settings.md` from `docs/configuration.md`'s `runtime`-kind rows -- see
`docs/stack.md`'s "Derived reference pages" section for the full contract. Generation, not
hand-copying, is what keeps the derived page from silently disagreeing with its source; this test is
what keeps a source edit that skips regeneration from shipping unnoticed, by actually running the
generator's `--check` mode -- the same one a human/CI would run -- against the real repo tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_derived_docs_are_up_to_date() -> None:
    result = subprocess.run(
        ["python3", str(REPO_ROOT / "scripts" / "generate_derived_docs.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "docs/keymap.md and/or docs/settings.md are stale relative to their source docs "
        "(docs/keybindings.md / docs/configuration.md) -- re-run "
        "`scripts/generate_derived_docs.py` and commit the result.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_gate_catches_a_stale_derived_page(tmp_path: Path) -> None:
    """Sabotage check: if `docs/keymap.md` (or `settings.md`) is not what the generator would
    currently produce, `--check` must actually fail, not silently pass. Runs the real generator
    against a scratch copy of the repo's docs/ + script, with the derived page truncated, so this
    proves the check's own generate-and-diff logic, not just that today's committed pages happen to
    match."""
    scratch_docs = tmp_path / "docs"
    scratch_scripts = tmp_path / "scripts"
    scratch_docs.mkdir()
    scratch_scripts.mkdir()
    for name in ("keybindings.md", "configuration.md"):
        (scratch_docs / name).write_text(
            (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")
        )
    (scratch_scripts / "generate_derived_docs.py").write_text(
        (REPO_ROOT / "scripts" / "generate_derived_docs.py").read_text(encoding="utf-8")
    )
    # A deliberately-stale keymap.md (a real generated one would never be empty).
    (scratch_docs / "keymap.md").write_text("stale\n", encoding="utf-8")
    (scratch_docs / "settings.md").write_text("stale\n", encoding="utf-8")

    result = subprocess.run(
        ["python3", str(scratch_scripts / "generate_derived_docs.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "keymap.md" in result.stdout + result.stderr
    assert "settings.md" in result.stdout + result.stderr

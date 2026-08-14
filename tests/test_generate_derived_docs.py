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
import sys
from pathlib import Path

from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package, so load by file path (same helper tests/test_check_links.py
# uses).
generate_derived_docs = load_module_from_path(
    "generate_derived_docs", REPO_ROOT / "scripts" / "generate_derived_docs.py"
)
# tests/test_check_links.py loads the same module, and `load_module_from_path` deliberately refuses
# to replace a live `sys.modules` entry -- so reuse whichever module object got there first.
check_links = sys.modules.get("check_links") or load_module_from_path(
    "check_links", REPO_ROOT / "scripts" / "check_links.py"
)


def test_derived_docs_are_up_to_date() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "generate_derived_docs.py"),
            "--check",
        ],
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


def _run_check_in_scratch_repo(
    tmp_path: Path, sources: dict[str, str], derived: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the real generator's `--check` against a scratch mirror of the repo, so a test can
    sabotage a source doc or a derived page without touching the working tree. `sources`/`derived`
    override a `docs/` file's text; any source not overridden is copied from the repo verbatim.

    The generator resolves `docs/` relative to its own file, so the script is copied in too.
    """
    scratch_docs = tmp_path / "docs"
    scratch_scripts = tmp_path / "scripts"
    scratch_docs.mkdir()
    scratch_scripts.mkdir()
    for name in ("keybindings.md", "configuration.md"):
        text = sources.get(
            name, (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")
        )
        (scratch_docs / name).write_text(text, encoding="utf-8")
    for name, text in derived.items():
        (scratch_docs / name).write_text(text, encoding="utf-8")
    script = scratch_scripts / "generate_derived_docs.py"
    script.write_text(
        (REPO_ROOT / "scripts" / "generate_derived_docs.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_gate_catches_a_stale_derived_page(tmp_path: Path) -> None:
    """Sabotage check: if `docs/keymap.md` (or `settings.md`) is not what the generator would
    currently produce, `--check` must actually fail, not silently pass -- proving the check's own
    generate-and-diff logic, not just that today's committed pages happen to match."""
    result = _run_check_in_scratch_repo(
        tmp_path,
        sources={},
        # A deliberately-stale pair (a real generated page would never be this).
        derived={"keymap.md": "stale\n", "settings.md": "stale\n"},
    )
    assert result.returncode == 1
    assert "keymap.md" in result.stdout + result.stderr
    assert "settings.md" in result.stdout + result.stderr


def test_a_renamed_source_heading_fails_loud(tmp_path: Path) -> None:
    """A source doc reshaped past what the generator can read must raise, not quietly emit a page
    missing the rows it could no longer find -- `--check` would then say only "stale, regenerate",
    and the truncated page would get committed."""
    keybindings = (REPO_ROOT / "docs" / "keybindings.md").read_text(encoding="utf-8")
    result = _run_check_in_scratch_repo(
        tmp_path,
        sources={
            "keybindings.md": keybindings.replace(
                "## Current keymap", "## The keymap as it stands"
            )
        },
        derived={},
    )
    assert result.returncode != 0
    assert "SourceDocChanged" in result.stderr


def test_a_renamed_source_column_fails_loud(tmp_path: Path) -> None:
    """Columns are read by NAME, so a renamed or reordered column raises too -- the failure a
    positional read would survive silently, emitting one column's content under another's header."""
    configuration = (REPO_ROOT / "docs" / "configuration.md").read_text(
        encoding="utf-8"
    )
    result = _run_check_in_scratch_repo(
        tmp_path,
        sources={
            "configuration.md": configuration.replace(
                "| Knob | Kind | Default | Notes |", "| Knob | Kind | Value | Notes |"
            )
        },
        derived={},
    )
    assert result.returncode != 0
    assert "SourceDocChanged" in result.stderr


def test_emitted_anchor_ids_match_the_link_gates_github_slug() -> None:
    """The generated `<a id=...>` must be the anchor GitHub itself derives from the same heading, so
    one `#link` resolves under both GitHub and the site's own (differently-slugged) renderer."""
    for heading in (
        "Paths & locations",
        "TUI — passive connection surfacing (E11)",
        "Retrieval and ranking",
        "Privacy & egress",
    ):
        assert generate_derived_docs._github_slug(heading) == check_links.github_slug(
            heading
        )

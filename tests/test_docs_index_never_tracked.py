"""Gate: the docs-index (`lode-t6o1`) can never become a tracked file
(lode-t6o1.4).

Two mechanisms enforce the "never tracked in git" constraint from
`lode-t6o1`'s design: the *structural* one -- `scripts/docs_index_build.py`'s
`cache_db_path()` resolves the build target outside this repo's worktree
(`lode-t6o1.2`) -- and this *gate* one. `.gitignore` alone is NOT a gate: a
later "let's just cache it in `.lode/`" change would silently defeat the
structural mechanism with nothing failing, and nobody would notice until two
branches that each ran the build collided on the committed artifact --
recreating `lode-4jtc`'s EOF collision one filename over (the epic's own
stated motivation). Same shape as `tests/test_bd_list_limit_gate.py` and
`tests/test_sweep_pipeline_label_roster_gate.py`: this fails on a NEW
violation, not a one-off assertion about today's paths.

Two independent checks, each SABOTAGE-VERIFIED below (a test proving the
check's own assertion helper actually fires, not just that today's repo
happens to be clean):

1. No index-artifact path is git-tracked (scans `git ls-files`).
2. The build target (`cache_db_path()`, default env) resolves outside this
   repository's worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Substrings looked for in the basename of every git-tracked file. Matches on
# the artifact's own name ("docs-index") and its file extension (a stray
# ".sqlite3"/".db" anywhere in the tree is itself suspicious -- this repo has
# no other sqlite-backed artifact that should ever be committed).
_INDEX_ARTIFACT_NAME_SUBSTRINGS = ("docs-index", "docs_index.sqlite")
_INDEX_ARTIFACT_SUFFIXES = (".sqlite3", ".sqlite", ".db")


def _find_index_artifacts(tracked_paths: list[str]) -> list[str]:
    """Return the subset of ``tracked_paths`` that look like a committed
    docs-index artifact. Pure function so the sabotage test below can drive
    it with a synthetic list, without needing to actually `git add` a file."""
    offenders = []
    for raw in tracked_paths:
        name = Path(raw).name
        if any(sub in name for sub in _INDEX_ARTIFACT_NAME_SUBSTRINGS) or any(
            name.endswith(suf) for suf in _INDEX_ARTIFACT_SUFFIXES
        ):
            offenders.append(raw)
    return offenders


def _assert_outside_worktree(path: Path) -> None:
    assert not path.is_relative_to(REPO_ROOT), (
        f"docs-index build target {path} resolves INSIDE the repo worktree -- "
        "this defeats the never-tracked constraint (lode-t6o1.4): every branch "
        "that ran the build would regenerate and could commit it, recreating "
        "lode-4jtc's EOF collision one filename over."
    )


def _load_build_module() -> object:
    # Loaded under a name private to THIS test module (not "docs_index_build",
    # which tests/test_docs_index_build.py already registers) -- collision
    # avoidance is load_module_from_path's own stated contract (see its
    # docstring in tests/conftest.py): registration is permanent for the
    # session, and a second load under an already-resident name asserts
    # loudly regardless of which of the two test files collects first.
    return load_module_from_path(
        "_docs_index_never_tracked_gate_build_impl",
        REPO_ROOT / "scripts" / "docs_index_build.py",
    )


# --- 1. No index artifact is git-tracked -----------------------------------


def _tracked_paths() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_no_index_artifact_is_git_tracked() -> None:
    tracked = _tracked_paths()
    # Non-vacuity pin (the sibling gates' `bd_lines > 0` move): without it, a
    # `git ls-files` that ever returned nothing -- wrong checkout, a future
    # pathspec filter -- would make this gate pass green forever with nothing
    # noticing.
    assert tracked, "git ls-files returned no paths -- this gate would be vacuous"
    offenders = _find_index_artifacts(tracked)
    assert offenders == [], (
        f"docs-index artifact(s) committed to git: {offenders} -- the index "
        "must only ever exist in the XDG cache dir, never in the worktree."
    )


def test_gate_catches_a_committed_index_artifact() -> None:
    """Sabotage check: if a future commit added a tracked index artifact,
    ``_find_index_artifacts`` must actually flag it, not silently pass."""
    tracked = ["docs/design.md", "scripts/docs_index_build.py", "docs-index.sqlite3"]
    assert _find_index_artifacts(tracked) == ["docs-index.sqlite3"]


def test_gate_catches_any_committed_sqlite_artifact_by_suffix() -> None:
    """A committed sqlite file under a different name is caught too -- the
    scan isn't keyed to today's exact filename."""
    tracked = ["docs/design.md", ".lode/cache.db"]
    assert _find_index_artifacts(tracked) == [".lode/cache.db"]


def test_gate_composes_real_discovery_with_the_match() -> None:
    """The two sabotage checks above drive the matcher with a hand-built list,
    which proves the matcher but not the *gate*. This one appends a synthetic
    offender to the REAL `git ls-files` output, so discovery and matching are
    exercised together -- the arrangement the live check actually runs."""
    tracked = [*_tracked_paths(), ".lode/docs-index.sqlite3"]
    assert _find_index_artifacts(tracked) == [".lode/docs-index.sqlite3"]


# --- 2. The build target resolves outside the worktree ---------------------


def test_default_build_target_resolves_outside_the_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    build = _load_build_module()
    _assert_outside_worktree(build.cache_db_path())


def test_gate_catches_a_build_target_moved_inside_the_worktree() -> None:
    """Sabotage check: if ``cache_db_path()`` ever regressed to returning a
    REPO_ROOT-relative path (e.g. a future "let's just cache it in .lode/"
    change), this gate's own assertion helper must fail loudly, not silently
    pass."""
    sabotaged = REPO_ROOT / ".lode" / "docs-index.sqlite3"
    with pytest.raises(AssertionError):
        _assert_outside_worktree(sabotaged)

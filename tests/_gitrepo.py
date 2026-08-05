"""Shared `_git` test helper for driving real throwaway git repos (lode-863q).

`_git(repo, *args)` was originally defined byte-identically (modulo the
parameter name `repo` vs `cwd`) in six shell-script test modules, and
extracted here so a fix to its error reporting or timeout didn't have to
land in every copy at once -- the same failure class
`scripts/recycled-worktree-guard.sh`'s four-copy inline-bash duplication had
before it was extracted into one script (lode-ivth), and the same reason
`tests/_hookharness.py` exists one tier up, after three copies of ITS harness
started to drift (lode-zlg8).

That six is HISTORICAL -- the count at extraction, not today's. The current
roster is deliberately NOT enumerated here: the enumeration that used to sit
in this paragraph went stale silently (it still said six well after four more
modules had imported this), and a new importer is the success case this
extraction was for, not drift. Read the roster off the code instead --
`grep -rl '^from _gitrepo import' tests/` (lode-c835).

`_init_repo`/`_add_worktree` are deliberately NOT hoisted here. Their
differences across modules -- a real `origin` remote vs none, branching off
`origin/trunk` vs bare `trunk`, an optional `foreign_commit` kwarg, different
fixture file content and commit messages -- encode the different contracts of
the script each module drives; flattening them into one parametrized helper
would likely be a net loss (lode-863q). Each module keeps its own.

A few `_init_repo`/`_add_worktree` bodies are duplicated pairwise across two
modules apiece. Which specific modules pair up, and whether the duplication
is byte-identical or merely identical modulo docstrings, is deliberately NOT
enumerated here -- this paragraph's own hand-typed inventory has already gone
stale three times (6f6ba9c, lode-9owc, lode-c835), the same failure class the
roster paragraph above was rewritten to stop having. Read the current
pairing off the code instead. Each such pair is left alone anyway: two
copies of a fixture premise sits under the three-copy bar both precedents
above fired at, and the pairs are NOT identical to EACH OTHER, so hoisting
would produce differently-shaped `_init_repo`s sharing one name across this
directory rather than one helper. Count the copies per PAIR, not in total,
when judging whether the bar has been crossed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result

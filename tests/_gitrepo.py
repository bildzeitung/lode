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

Some `_init_repo`/`_add_worktree` bodies are duplicated across modules. Which
modules share a body, how many do, and whether the duplication is
byte-identical or merely identical modulo docstrings, are deliberately NOT
enumerated here -- this paragraph's own hand-typed inventory has already gone
stale three times (6f6ba9c, lode-9owc, lode-c835), the same failure class the
roster paragraph above was rewritten to stop having. Measure it off the code
when it matters, and count the copies of ONE body, not the copies in total:
two differently-shaped `_init_repo`s duplicated twice each is not four copies
of anything, and hoisting them under one name would leave this directory with
differently-shaped `_init_repo`s sharing that name rather than one helper. A
body under the three-copy bar both precedents above fired at is left alone.

This question was asked directly, by name, in lode-ea5b (the residual of
lode-y8u0 that lode-863q's own scope note deferred): is there an honest
shared builder across the (by then seven) `_init_repo` copies, or should they
stay separate? Re-measured there against seven bodies -- same rule, same
outcome, nothing hoisted. That finding is deliberately not repeated as a
count here, for the same staleness reason the roster paragraphs above give;
re-measure per-module when it next matters rather than trusting a number
written down once.
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

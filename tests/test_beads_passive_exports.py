"""The canonical beads passive-export list, and the chain that reaches it (lode-do3q).

`scripts/beads-passive-exports.txt` is the single copy of the two relpaths that the guards
must treat as "by invariant never real work" (`import.auto: false`, lode-6ra). Its consumers
read it rather than re-inlining the paths: `scripts/worktree-gc-classify.sh`'s dirty-tree guard,
the `Stop` hook in `.claude/settings.json` (via
`scripts/discard-beads-passive-export-churn.sh`), `scripts/land-merge-one.sh`'s merge-retry
restore (lode-2nw5), and `scripts/land-replay.sh`'s two dirty-tree reformat-detect checks
(lode-3cda). Register a new consumer in the loops below rather than starting a parallel module
for it.

(A fourth consumer, `tests/test_land_lock.py`'s `_STALL_HOOK_SCAN_EXCLUDED_RELPATHS`, is gone
as of lode-y3dw: `flock(1)` replaced the mkdir reclaim gate, retiring the
`LAND_LOCK_TEST_STALL_SECONDS` hook that scan existed to police. `docs/decisions.md`'s lode-do3q
entry still names it -- correctly, since that file records decisions as of their date and is
appended to rather than rewritten.)

Canonicalizing bought a one-file edit, but it also bought a NEW failure surface the previous
inline copies could not have: an indirection chain (settings.json -> script -> data file) whose
every link swallows its own errors. The `Stop` hook ends in `; true`, and
`discard-beads-passive-export-churn.sh` documents that it always exits 0 -- correct for
best-effort hygiene, but it means a renamed or deleted script leaves the hook a permanent no-op
with nothing red anywhere. These tests pin the chain so the canonicalization cannot rot into a
silently-disconnected one. Same shape as `tests/test_gh_write_guard.py`'s assertion that its
hook still names its script.
"""

from __future__ import annotations

import json
import os

from _hookharness import SETTINGS

REPO_ROOT = SETTINGS.parent.parent
CANONICAL_LIST = REPO_ROOT / "scripts" / "beads-passive-exports.txt"
HOOK_SCRIPT = REPO_ROOT / "scripts" / "discard-beads-passive-export-churn.sh"
GC_CLASSIFY = REPO_ROOT / "scripts" / "worktree-gc-classify.sh"
MERGE_ONE = REPO_ROOT / "scripts" / "land-merge-one.sh"
LAND_REPLAY = REPO_ROOT / "scripts" / "land-replay.sh"


def _entries() -> list[str]:
    return CANONICAL_LIST.read_text(encoding="utf-8").splitlines()


def test_the_canonical_list_is_present_and_every_line_is_a_usable_relpath() -> None:
    """Non-vacuity for all three consumers at once.

    Each consumer degrades differently on a bad list -- the gc classifier now exits 2, the
    Stop hook no-ops, the stall-hook scan silently widens -- so the list itself is the one
    place worth asserting the precondition all three share.
    """
    assert CANONICAL_LIST.is_file()
    entries = _entries()
    assert entries, "an empty list would silently disarm every consumer"
    for rel in entries:
        assert rel == rel.strip() != "", (
            f"blank or padded entry {rel!r} becomes a bad pathspec"
        )
        assert not rel.startswith("/"), f"{rel!r} must be repo-relative, not absolute"
        assert rel.startswith(".beads/"), f"{rel!r} is not a beads passive export"


def test_the_stop_hook_still_reaches_an_executable_script() -> None:
    """The link that can rot silently: the hook swallows a missing script via `; true`."""
    settings = json.loads(SETTINGS.read_text())
    stop_commands = [
        h["command"] for entry in settings["hooks"]["Stop"] for h in entry["hooks"]
    ]
    matching = [c for c in stop_commands if HOOK_SCRIPT.name in c]
    assert len(matching) == 1, (
        f"expected exactly one Stop hook naming {HOOK_SCRIPT.name}"
    )
    assert HOOK_SCRIPT.is_file(), "the Stop hook names a script that does not exist"
    assert os.access(HOOK_SCRIPT, os.X_OK), "the Stop hook's script is not executable"


def test_no_consumer_keeps_a_literal_copy_of_the_relpaths() -> None:
    """The anti-drift invariant the canonicalization actually bought.

    Re-inlining any relpath at a consumer is exactly the three-copies-in-three-syntaxes state
    lode-do3q removed, and would be invisible -- the re-inlined copy would agree with the
    canonical file on the day it was written.
    """
    settings = json.loads(SETTINGS.read_text())
    stop_commands = " ".join(
        h["command"] for entry in settings["hooks"]["Stop"] for h in entry["hooks"]
    )
    consumers = {
        ".claude/settings.json (Stop hooks)": stop_commands,
        str(HOOK_SCRIPT): HOOK_SCRIPT.read_text(encoding="utf-8"),
        str(GC_CLASSIFY): GC_CLASSIFY.read_text(encoding="utf-8"),
        str(MERGE_ONE): MERGE_ONE.read_text(encoding="utf-8"),
        str(LAND_REPLAY): LAND_REPLAY.read_text(encoding="utf-8"),
    }
    for rel in _entries():
        for name, text in consumers.items():
            assert rel not in text, f"{name} re-inlines the canonical relpath {rel!r}"


def test_every_bash_consumer_names_the_canonical_list() -> None:
    """Cheap proof the scripts read the file this module is asserting about, so a rename
    of the list cannot leave these tests green while the guards read nothing."""
    for script in (HOOK_SCRIPT, GC_CLASSIFY, MERGE_ONE, LAND_REPLAY):
        assert CANONICAL_LIST.name in script.read_text(encoding="utf-8"), (
            f"{script} no longer reads {CANONICAL_LIST.name}"
        )


def test_land_replay_no_longer_hardcodes_the_beads_pathspec() -> None:
    """lode-3cda: land-replay.sh used to hardcode a literal ':!.beads' git pathspec
    argument, which is BROADER than the canonical list -- it excluded the whole
    .beads/ directory, so a real non-passive .beads/ change (e.g. config.yaml) was
    invisible to both dirty-tree checks. Checks actual code lines only (a `git diff`
    call ending in the literal pathspec), not this file's own prose describing the fix."""
    code_lines = [
        line
        for line in LAND_REPLAY.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    assert not any(":!.beads" in line for line in code_lines), (
        "land-replay.sh still hardcodes the broad ':!.beads' pathspec in code instead of "
        f"reading {CANONICAL_LIST.name}"
    )

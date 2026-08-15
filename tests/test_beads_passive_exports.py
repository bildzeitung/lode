"""The canonical beads passive-export list, and the chain that reaches it (lode-do3q).

`scripts/beads-passive-exports.txt` is the single copy of the two relpaths that the guards
must treat as "by invariant never real work" (`import.auto: false`, lode-6ra). Its consumers
read it rather than re-inlining the paths: `scripts/worktree-gc-classify.sh`'s dirty-tree guard,
the `Stop` hook in `.claude/settings.json` (via
`scripts/discard-beads-passive-export-churn.sh`), `scripts/land-merge-one.sh`'s merge-retry
restore (lode-2nw5), and `scripts/land-replay.sh`'s two dirty-tree reformat-detect checks
(lode-3cda). Register a new bash consumer in the `BASH_CONSUMERS` tuple below -- every
per-consumer test derives from it -- rather than starting a parallel module for it.

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
import re
import subprocess
from pathlib import Path

import pytest
from _hookharness import SETTINGS

REPO_ROOT = SETTINGS.parent.parent
CANONICAL_LIST = REPO_ROOT / "scripts" / "beads-passive-exports.txt"
HOOK_SCRIPT = REPO_ROOT / "scripts" / "discard-beads-passive-export-churn.sh"
GC_CLASSIFY = REPO_ROOT / "scripts" / "worktree-gc-classify.sh"
MERGE_ONE = REPO_ROOT / "scripts" / "land-merge-one.sh"
LAND_REPLAY = REPO_ROOT / "scripts" / "land-replay.sh"

# The one registry every per-consumer test below derives from -- registering a new bash
# consumer is a single edit here, not one per loop (lode-3cda's review: the module had
# grown three hand-maintained copies of this set).
BASH_CONSUMERS = (HOOK_SCRIPT, GC_CLASSIFY, MERGE_ONE, LAND_REPLAY)

# The sourced library that now OWNS the load+validate+":(exclude)" transform for two of
# those consumers (lode-xlcm). It is not a member of BASH_CONSUMERS -- it never names the
# canonical list in code (its callers pass the path in), so
# test_every_bash_consumer_names_the_canonical_list does not apply to it. The two CONTENT
# invariants below do: they are what stops a relpath or the broad ':!.beads' pathspec from
# being re-inlined, and after the extraction the place that would land is this file, one
# level below every consumer. Registering it here rather than in BASH_CONSUMERS keeps each
# invariant asserted over exactly the files it is true of.
PASSIVE_EXPORTS_LIB = REPO_ROOT / "scripts" / "beads-passive-exports.sh"
PATHSPEC_OWNERS = BASH_CONSUMERS + (PASSIVE_EXPORTS_LIB,)


def _entries() -> list[str]:
    return CANONICAL_LIST.read_text(encoding="utf-8").splitlines()


def test_the_canonical_list_is_present_and_every_line_is_a_usable_relpath() -> None:
    """Non-vacuity for every consumer at once.

    Each consumer degrades differently on a bad list -- the gc classifier now exits 2, the
    Stop hook no-ops, the stall-hook scan silently widens -- so the list itself is the one
    place worth asserting the precondition they all share.
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
    consumers = {".claude/settings.json (Stop hooks)": stop_commands} | {
        str(script): script.read_text(encoding="utf-8") for script in PATHSPEC_OWNERS
    }
    for rel in _entries():
        for name, text in consumers.items():
            assert rel not in text, f"{name} re-inlines the canonical relpath {rel!r}"


def test_every_bash_consumer_names_the_canonical_list() -> None:
    """Cheap proof the scripts read the file this module is asserting about, so a rename
    of the list cannot leave these tests green while the guards read nothing."""
    for script in BASH_CONSUMERS:
        assert CANONICAL_LIST.name in script.read_text(encoding="utf-8"), (
            f"{script} no longer reads {CANONICAL_LIST.name}"
        )


def test_no_consumer_hardcodes_the_broad_beads_pathspec() -> None:
    """lode-3cda: land-replay.sh used to hardcode a literal ':!.beads' git pathspec
    argument, which is BROADER than the canonical list -- it excluded the whole
    .beads/ directory, so a real non-passive .beads/ change (e.g. config.yaml) was
    invisible to both of its dirty-tree checks. The invariant is general, so it is
    asserted over every bash consumer rather than the one script that regressed.
    Checks actual code lines only, not a comment describing the fix."""
    for script in PATHSPEC_OWNERS:
        code_lines = [
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        ]
        assert not any(":!.beads" in line for line in code_lines), (
            f"{script} hardcodes the broad ':!.beads' pathspec in code instead of "
            f"reading {CANONICAL_LIST.name}"
        )


def _lib_sourcers() -> list[Path]:
    """Every scripts/*.sh that sources the library, DISCOVERED rather than listed.

    Same reasoning as `tests/test_gate_lib.py`'s `_sources_gate_lib` (lode-pcee): a
    hardcoded roster cannot fail on the newcomer these sweeps exist to catch. Discovery is
    on the library's filename in a non-comment line, deliberately NOT on the guard text --
    tightening it onto the guard would vacate the guard sweep below, which is the whole
    point of the discovery.
    """
    found = []
    for script in sorted((REPO_ROOT / "scripts").glob("*.sh")):
        if script == PASSIVE_EXPORTS_LIB:
            continue
        code = [
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        ]
        if any(PASSIVE_EXPORTS_LIB.name in line for line in code):
            found.append(script)
    return found


def test_the_library_has_at_least_the_two_sourcers_it_was_extracted_for() -> None:
    """Non-vacuity for the guard sweep below: if discovery ever returns nothing, that sweep
    passes trivially while enforcing nothing."""
    assert set(_lib_sourcers()) >= {GC_CLASSIFY, LAND_REPLAY}


@pytest.mark.parametrize("script", _lib_sourcers(), ids=lambda p: p.name)
def test_every_sourcer_guards_the_source_line(script: Path) -> None:
    """lode-bss5's fail-closed convention, applied to this library (lode-xlcm).

    A bare `. "$SCRIPT_DIR/beads-passive-exports.sh"` under `set -uo pipefail` does NOT stop
    the script when the source fails -- it leaves `load_beads_passive_exports` undefined and
    the first call site resolves to a bash "command not found" whose exit code is whatever
    the surrounding logic happens to produce, never the 2 the caller meant. A third consumer
    that sources bare fails here the day it lands rather than on a live gate.
    """
    for line in script.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or PASSIVE_EXPORTS_LIB.name not in stripped:
            continue
        if re.search(r"^\.\s|(^|\s)\.\s+\"", stripped):
            assert stripped.startswith("if ! . "), (
                f"{script.name} sources {PASSIVE_EXPORTS_LIB.name} without a fail-closed "
                f"guard: {stripped!r}"
            )


def _load(tmp_path: Path, list_body: str | None) -> subprocess.CompletedProcess[str]:
    """Source the library in a fresh bash and call `load_beads_passive_exports`.

    Prints the return code, then the two globals -- so an assertion can distinguish "set"
    from "left over from a failed load", which the header now documents as undefined.
    """
    list_path = tmp_path / "list.txt"
    if list_body is not None:
        list_path.write_text(list_body, encoding="utf-8")
    script = f"""
      set -uo pipefail
      . "{PASSIVE_EXPORTS_LIB}"
      load_beads_passive_exports "{list_path}"
      echo "rc=$?"
      echo "pathspecs=${{BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS[*]-<unset>}}"
    """
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_a_good_list_loads_and_becomes_exclude_pathspecs(tmp_path: Path) -> None:
    """The transform the extraction moved out of two scripts, asserted once, here."""
    result = _load(tmp_path, ".beads/issues.jsonl\n.beads/interactions.jsonl\n")
    assert "rc=0" in result.stdout, result.stdout + result.stderr
    assert (
        "pathspecs=:(exclude).beads/issues.jsonl :(exclude).beads/interactions.jsonl"
        in result.stdout
    )


@pytest.mark.parametrize(
    ("body", "cause"),
    [
        (None, "cannot read"),
        ("", "empty or contains a blank line"),
        (
            ".beads/issues.jsonl\n\n.beads/interactions.jsonl\n",
            "empty or contains a blank line",
        ),
    ],
    ids=["unreadable", "empty", "blank-line"],
)
def test_a_bad_list_returns_1_with_one_diagnostic_and_never_exits(
    tmp_path: Path, body: str | None, cause: str
) -> None:
    """The contract every caller's own failure handling is layered on: a plain non-zero
    RETURN (so the caller chooses gate_could_not_run vs echo+exit 2 vs the Stop hook's
    deliberate exit 0), never an `exit` taken on the caller's behalf. The trailing `echo`s
    in `_load` are what prove the calling shell survived the failure.
    """
    result = _load(tmp_path, body)
    assert "rc=1" in result.stdout, result.stdout + result.stderr
    diagnostics = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert len(diagnostics) == 1, result.stderr
    assert cause in diagnostics[0]
    assert str(tmp_path / "list.txt") in diagnostics[0]

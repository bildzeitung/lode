"""Pins the shared idioms of scripts/land-merge-batch.sh and scripts/land-replay.sh
byte-for-byte equal between the two files (lode-fdod).

`/land`'s Section 3 has two merge loops with the same shape -- the first-pass
batch merge (land-merge-batch.sh) and the isolation-replay loop
(land-replay.sh) -- kept as two separate scripts rather than unified into one
(decision recorded in docs/decisions.md under lode-fdod: the two loops'
verdict sets genuinely differ -- LANDED/CONFLICT/HELD vs.
LANDED/CONFLICT/BOUNCED/HELD, and only the replay loop gates per-branch --
so a shared loop body would need its own conditional gating mode threaded
through the most destructive code path in the repo).

What was never enforced is that the two loops' remaining SHARED idioms --
copied by hand when land-replay.sh was extracted -- stay identical. This test
closes that gap mechanically: it pins the two idioms both scripts' headers
call out explicitly as "the same two traps, learned the hard way" --

  * the `grep -qxF "$id" "$ACCEPTED"` / `grc=$?` / `case "$grc" in 0) ;; 1)
    continue ;;` stale-membership re-check (the 0/1/else partition).
  * the `if CMD; then rc=0; else rc=$?; fi` merge-dispatch idiom (NOT the
    negated `if ! CMD; then rc=$?; fi` form, which would silently read a
    machine-fault exit 2 as a clean merge).

as exact substrings that must appear, byte-for-byte, in BOTH scripts. An edit
to either idiom in one file that is not mirrored in the other now fails this
test instead of only being caught by someone reading both files side by side.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BATCH_SCRIPT = REPO_ROOT / "scripts" / "land-merge-batch.sh"
REPLAY_SCRIPT = REPO_ROOT / "scripts" / "land-replay.sh"

# The stale-membership re-check idiom, minus the one line that legitimately
# differs between the two loops (the *) arm's diagnostic message names
# "batch" vs. "replay") -- everything else in the 0/1/else partition is
# required to be identical.
GREP_RECHECK_IDIOM = (
    '  grep -qxF "$id" "$ACCEPTED"\n'
    "  grc=$?\n"
    '  case "$grc" in\n'
    "    0) ;;\n"
    "    1) continue ;;\n"
)

# The merge-dispatch idiom: `if CMD; then rc=0; else rc=$?; fi`, never the
# negated form. Fully identical in both scripts, docstrings included in each
# file's header as the reason this exact shape matters.
MERGE_DISPATCH_IDIOM = (
    '  if CONFLICTS=$("$SCRIPT_DIR/land-merge-one.sh" "$id" "$MSG_DIR" "$TOKEN"); then\n'
    "    rc=0\n"
    "  else\n"
    "    rc=$?\n"
    "  fi\n"
)


def test_grep_recheck_idiom_present_in_both_scripts() -> None:
    batch_text = BATCH_SCRIPT.read_text()
    replay_text = REPLAY_SCRIPT.read_text()
    assert GREP_RECHECK_IDIOM in batch_text, (
        "land-merge-batch.sh's stale-membership re-check drifted from the pinned idiom -- "
        "update this test deliberately if the drift was intentional, and check "
        "land-replay.sh's copy is still consistent."
    )
    assert GREP_RECHECK_IDIOM in replay_text, (
        "land-replay.sh's stale-membership re-check drifted from the pinned idiom -- "
        "update this test deliberately if the drift was intentional, and check "
        "land-merge-batch.sh's copy is still consistent."
    )


def test_merge_dispatch_idiom_present_in_both_scripts() -> None:
    batch_text = BATCH_SCRIPT.read_text()
    replay_text = REPLAY_SCRIPT.read_text()
    assert MERGE_DISPATCH_IDIOM in batch_text, (
        "land-merge-batch.sh's merge-dispatch idiom drifted -- this is the exact shape "
        "(`if CMD; then rc=0; else rc=$?; fi`) that avoids silently reading a machine-fault "
        "exit 2 as a clean merge; do not weaken it back to the negated form."
    )
    assert MERGE_DISPATCH_IDIOM in replay_text, (
        "land-replay.sh's merge-dispatch idiom drifted -- this is the exact shape "
        "(`if CMD; then rc=0; else rc=$?; fi`) that avoids silently reading a machine-fault "
        "exit 2 as a clean merge; do not weaken it back to the negated form."
    )

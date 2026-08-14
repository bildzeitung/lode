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

  * the `grep -qxF "$id" "$ACCEPTED"` / `grc=$?` / `case "$grc" in` ...
    stale-membership re-check, pinned across all three arms of the 0/1/else
    partition (the else arm included -- see its comment below).
  * the `if CMD; then rc=0; else rc=$?; fi` merge-dispatch idiom (NOT the
    negated `if ! CMD; then rc=$?; fi` form, which would silently read a
    machine-fault exit 2 as a clean merge).

as exact substrings that must appear, byte-for-byte, in BOTH scripts. An edit
to either idiom in one file that is not mirrored in the other now fails this
test instead of only being caught by someone reading both files side by side.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BATCH_SCRIPT = REPO_ROOT / "scripts" / "land-merge-batch.sh"
REPLAY_SCRIPT = REPO_ROOT / "scripts" / "land-replay.sh"

# The stale-membership re-check idiom. The pin runs THROUGH the `*)` arm's
# opening line, not just the 0/1 arms: the else arm is the whole point of the
# partition (a grep that fails for any reason other than "absent" must be a
# machine fault, never read as "already dropped"), and stopping the pin one
# line short would let that arm be deleted from either script with the test
# still green. Only the arm's CONTINUATION line legitimately differs between
# the two loops -- its diagnostic names "batch" vs. "replay" -- so the pin
# ends at the backslash.
GREP_RECHECK_IDIOM = (
    '  grep -qxF "$id" "$ACCEPTED"\n'
    "  grc=$?\n"
    '  case "$grc" in\n'
    "    0) ;;\n"
    "    1) continue ;;\n"
    "    *) gate_could_not_run \"grep failed (exit $grc) re-checking '$id' in '$ACCEPTED'\" \\\n"
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


# name -> (pinned text, why this exact shape matters). The "why" is what the
# failure message carries, so whoever trips this test learns what the drift
# would have cost rather than just which bytes moved.
PINNED_IDIOMS = {
    "stale-membership re-check": (
        GREP_RECHECK_IDIOM,
        (
            "the 0/1/else partition that keeps a grep failure from being read as "
            "'already dropped' and silently skipping the rest of the loop"
        ),
    ),
    "merge-dispatch": (
        MERGE_DISPATCH_IDIOM,
        (
            "`if CMD; then rc=0; else rc=$?; fi` -- never the negated form, which "
            "would silently read land-merge-one.sh's machine-fault exit 2 as a clean merge"
        ),
    ),
}


@pytest.mark.parametrize("script", [BATCH_SCRIPT, REPLAY_SCRIPT], ids=lambda p: p.name)
@pytest.mark.parametrize("name", sorted(PINNED_IDIOMS))
def test_idiom_pinned_in_both_scripts(name: str, script: Path) -> None:
    idiom, why = PINNED_IDIOMS[name]
    assert idiom in script.read_text(), (
        f"{script.name}'s {name} idiom drifted from the pinned copy: {why}. "
        "The two loops are deliberately NOT unified (docs/decisions.md, lode-fdod), so this "
        "test is the only thing keeping their hand-copied idioms in sync -- if the drift was "
        "intentional, mirror it in the other script and update the pin here deliberately."
    )

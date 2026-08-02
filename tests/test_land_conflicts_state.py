"""Pins lode-rfon's fix: $CONFLICTS must survive from its producer to the
"Needs rebase -- kick back" consumer in `.claude/skills/land/SKILL.md`.

An agent executing this skill runs each fenced ```bash block as its own,
separate Bash tool invocation -- shell variables do not survive between them
(the same defect class lode-sfnb closed for $MSG/$ACCEPTED/$LANDED). Before
this fix, $CONFLICTS was captured in Section 2b's `merge-precheck.sh` call and
in each of Section 3's two `land-merge-one.sh` merge loops, but the "Needs
rebase -- kick back" block that interpolates it into a `bd --append-notes` is
a later, separate block -- so $CONFLICTS read there was always empty, and the
kick-back note silently got a blank line where the conflicting paths belong.

The fix persists $CONFLICTS to a file under `$STATE_DIR/conflicts/<id>`
(`.git/land-state/conflicts/<id>`) at each producer site, reusing the
$STATE_DIR mechanism lode-sfnb already established for the accepted/landed/msg
files, and has the kick-back block read it back from disk -- refusing loudly
(never with a blank paths section) if that file is missing or empty.

Pinned against the SHIPPED SKILL.md, not a reimplementation (the
tests/test_land_lock.py precedent) -- the bug lived in a markdown fence no
gate parses, so only reading the actual file catches a regression.
"""

from __future__ import annotations

from pathlib import Path

from conftest import bash_fence_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent
LAND_SKILL = REPO_ROOT / ".claude" / "skills" / "land" / "SKILL.md"


def _skill_blocks() -> list[str]:
    """Each ```bash fence as its own string, in document order -- what an
    agent actually EXECUTES, one Bash tool invocation per block.

    Unlike a single concatenation of every fenced block (the
    tests/test_land_lock.py precedent), this preserves block BOUNDARIES,
    which is the point here: whether $CONFLICTS is read from a file or from a
    bash variable set earlier is a per-block question. Thin wrapper over the
    shared tests/conftest.py::bash_fence_blocks parser, onto which lode-ovgs
    unified this file's own former private copy (byte-identical in behaviour
    on every consumed file -- verified, so this file's pins policed exactly the
    same block set before and after). The parser's known blind spots are
    documented next to it, not restated here.
    """
    return bash_fence_blocks(LAND_SKILL.read_text(encoding="utf-8"))


def _only_block_with(*needles: str, what: str) -> str:
    """The single fenced block containing every needle.

    Asserts there is EXACTLY one. `next(..., None)` would silently pin the
    first of several near-identical blocks, which in this file is a live
    hazard: Section 3's two merge loops are deliberately the same shape, so a
    locator that stops at the first match would pin one loop and let the other
    regress unseen -- the precise failure the Section-3 test exists to catch.
    """
    hits = [b for b in _skill_blocks() if all(n in b for n in needles)]
    assert len(hits) == 1, (
        f"expected exactly 1 fenced block for {what}, found {len(hits)} -- this "
        "test's assumption about SKILL.md's structure has drifted; re-check by "
        "hand before adjusting the locator"
    )
    return hits[0]


def _kick_back_block() -> str:
    return _only_block_with(
        "--add-label needs-rebase",
        "rtk bd update",
        what="the needs-rebase kick-back",
    )


def test_2b_precheck_persists_conflicts_to_the_state_dir() -> None:
    """Section 2b's merge-precheck.sh call is the COMMON producer path -- it
    pre-dates lode-sfnb, runs on every branch every pass, and the ticket is
    explicit that a fix covering only Section 3 misses the frequent case."""
    site = _only_block_with(
        'merge-precheck.sh origin/trunk "origin/land/<id>"',
        what="Section 2b's merge-precheck.sh call",
    )
    assert 'CONFLICTS_DIR="$STATE_DIR/conflicts"' in site, (
        "2b no longer derives CONFLICTS_DIR -- lode-rfon's fix regressed"
    )
    assert 'printf \'%s\\n\' "$CONFLICTS" > "$CONFLICTS_DIR/<id>"' in site, (
        "2b's rc=1 branch no longer persists $CONFLICTS to disk -- the kick-back "
        "block (a separate Bash invocation) would see an empty variable again "
        "(lode-rfon)"
    )
    assert 'if [ "$rc" = 1 ]; then' in site, (
        "2b's persist is no longer guarded by an `if`. It must NOT be written as "
        '`[ "$rc" = 1 ] && printf ...`: as this block\'s LAST command that AND-list '
        "makes the whole Bash invocation exit 1 on the COMMON clean path (rc=0) and "
        "exit 0 on a real conflict -- inverting the block's exit status, which is the "
        "only signal it gives the agent at all (it prints nothing on any path). "
        "Verified empirically, with and without `set -e` (lode-rfon technical review)."
    )


def test_section_3_merge_loops_both_persist_conflicts_to_the_state_dir() -> None:
    """Section 3 has TWO merge loops -- the first pass, and the
    isolation-replay loop entered on a red combined re-gate. Both call
    scripts/land-merge-one.sh and both must persist a real conflict's paths
    the same way, or the isolation-replay path silently regresses even when
    the first-pass loop is fixed."""
    sites = [b for b in _skill_blocks() if "scripts/land-merge-one.sh" in b]
    assert len(sites) == 2, (
        f"expected exactly 2 fenced blocks calling land-merge-one.sh (Section 3's "
        f"two merge loops), found {len(sites)} -- this test's assumption about "
        "SKILL.md's structure has drifted; re-check by hand before adjusting the count"
    )
    for i, site in enumerate(sites, start=1):
        assert 'CONFLICTS_DIR="$STATE_DIR/conflicts"' in site, (
            f"merge loop #{i} no longer (re-)derives CONFLICTS_DIR"
        )
        assert 'printf \'%s\\n\' "$CONFLICTS" > "$CONFLICTS_DIR/$id"' in site, (
            f"merge loop #{i}'s rc=1 arm no longer persists $CONFLICTS to disk "
            "(lode-rfon)"
        )


def test_kick_back_block_reads_conflicts_from_disk_not_a_bare_variable() -> None:
    """The exact regression lode-rfon fixed: the 'Needs rebase -- kick back'
    block interpolated a bare $CONFLICTS into a bd --append-notes without ever
    setting it IN THAT SAME BLOCK. It is a separate Bash invocation from every
    producer above, so a bare $CONFLICTS there is always empty."""
    site = _kick_back_block()
    assert 'CONFLICTS=$(cat "$STATE_DIR/conflicts/<id>"' in site, (
        "the kick-back block no longer reads $CONFLICTS back from "
        "$STATE_DIR/conflicts/<id> -- it must not rely on a bash variable set in "
        "an earlier, separate Bash invocation (lode-rfon)"
    )

    read_pos = site.index('CONFLICTS=$(cat "$STATE_DIR/conflicts/<id>"')
    update_pos = site.index("rtk bd update <id> --remove-label ready-for-land")
    assert read_pos < update_pos, (
        "the kick-back block reads $CONFLICTS from disk AFTER the bd update call "
        "-- too late to be interpolated into the --append-notes text"
    )


def test_kick_back_block_refuses_loudly_on_missing_or_empty_conflicts() -> None:
    """Acceptance criterion (lode-rfon): an empty/missing conflicts record
    must be LOUD, never a kick-back note with a blank paths section."""
    site = _kick_back_block()
    assert "GATE COULD NOT RUN" in site, (
        "the kick-back block has no loud failure message for a missing/empty "
        "conflicts record -- a blank $CONFLICTS could silently reach the "
        "--append-notes text again"
    )

    guard_pos = site.index("GATE COULD NOT RUN")
    update_pos = site.index("rtk bd update <id> --remove-label ready-for-land")
    exit_pos = site.index("exit 1", guard_pos)
    assert guard_pos < exit_pos < update_pos, (
        "the loud failure guard does not exit BEFORE the bd update call -- a "
        "missing/empty conflicts record could still produce a kick-back note "
        "with a blank paths section"
    )

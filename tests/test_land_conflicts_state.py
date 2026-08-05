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

lode-wjw4 extended this file to the OTHER half of the same mechanism: WHERE
the per-pass `rm -rf "$STATE_DIR"` sits. It used to run in Section 3a, three
sections after the pass began and after 2b had already written its conflicts
record, so 2b was ordered IN PROSE to kick back before 3a ran or lose the
file. The wipe is hoisted to Section 1, ahead of every writer, which makes
that ordering structural instead of remembered -- and the pins below are what
keep it that way, since nothing else in this repo parses this markdown.

lode-youi generalized this file's charter accordingly: it now owns DOCUMENT-ORDER
invariants of `.claude/skills/land/SKILL.md` that no other gate parses, not only
the $CONFLICTS/$STATE_DIR ones the paragraphs above describe. The pin added there
(Section 3's re-gate precedes Section 4's `origin/trunk` push) is about neither
$CONFLICTS nor $STATE_DIR; it lives here because this is where the block-boundary
scaffolding and the cross-block-ordering precedent already are.
"""

from __future__ import annotations

from conftest import LAND_SKILL, bash_fence_blocks


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
        "bd update",
        what="the needs-rebase kick-back",
    )


def test_state_dir_is_wiped_once_in_section_1_ahead_of_every_writer() -> None:
    """lode-wjw4: the per-pass `rm -rf "$STATE_DIR"` lives in Section 1's
    setup fence -- the first block of the pass -- and nowhere else.

    Below any writer it destroys that writer's state mid-pass: the observed
    victim was 2b's conflicts record, which the kick-back block reads back
    from disk (the tests above), and which 3a's wipe deleted whenever the
    kick-back was deferred past it. Hoisting the wipe is what removed that
    ordering constraint; prose alone would let a future editor put it back.

    `_only_block_with`'s exactly-one assertion is deliberately also sensitive
    to a COMMENT quoting the literal wipe -- that is how origin/trunk's shape
    scored two matches (2b carried a comment naming 3a's wipe). A comment
    restating where the wipe lives is the same remembered-not-structural
    pattern this ticket deleted, so re-flagging it is the intent, not a
    false positive.
    """
    site = _only_block_with('rm -rf "$STATE_DIR"', what="the per-pass $STATE_DIR wipe")
    assert "git checkout -f trunk" in site, (
        "the $STATE_DIR wipe is no longer in Section 1's setup fence (the only "
        "block that runs `git checkout -f trunk`). Anywhere later and a block "
        "that writes under $STATE_DIR before it -- 2b's conflicts record, 3a's "
        "accepted/msg files -- loses that state to it (lode-wjw4)"
    )


def test_section_1_block_still_ends_on_the_pass_start_reset() -> None:
    """The wipe must not become Section 1's LAST command (lode-wjw4).

    No fenced block in this file runs under `set -e` (the governing rule at
    the top: not even `set -e` carries between blocks), so a block's exit
    status is its last command's and nothing else. `rm -rf` reports success
    even on a path that does not exist -- measured, a failed `git reset
    --hard origin/trunk` goes from rc 128 to a block that exits 0 -- which
    would let the pass spend N `land-review` dispatches, a full re-gate and a
    `git push origin trunk` on top of un-reset residue. Same hazard 2b's own
    `if [ "$rc" = 1 ]` comment reasons about, from the other direction.
    """
    site = _only_block_with(
        "git reset --hard origin/trunk",
        "git checkout -f trunk",
        what="Section 1's pass-start block",
    )
    last = [
        ln for ln in site.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ][-1]
    assert last.startswith("git reset --hard origin/trunk"), (
        "Section 1's setup block no longer ends on `git reset --hard "
        f"origin/trunk` -- its last executed line is {last.strip()!r}. That "
        "command's exit status is the only machine-readable signal the block "
        "gives; ending on anything that cannot fail hides a failed reset "
        "(lode-wjw4)"
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
    update_pos = site.index("bd update <id> --remove-label ready-for-land")
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
    update_pos = site.index("bd update <id> --remove-label ready-for-land")
    exit_pos = site.index("exit 1", guard_pos)
    assert guard_pos < exit_pos < update_pos, (
        "the loud failure guard does not exit BEFORE the bd update call -- a "
        "missing/empty conflicts record could still produce a kick-back note "
        "with a blank paths section"
    )


_REGATE = "nox -s tests"
"""The re-gate needle: the session that actually gates CONTENT.

Not `nox -s lock_currency`, which matches the same two blocks today (measured)
but whose exit 2 SKILL.md explicitly treats as NOT a red gate, and which is the
chain's newest and most volatile member (lode-sys4). Pinning on it would go red
when someone drops it for the reason the doc already contemplates, and stay
green if `nox -s tests` were removed -- both directions wrong.
"""

_PUSH = "git push origin trunk"


def _regate_and_push_indices(blocks: list[str]) -> tuple[list[int], int]:
    """Document-order positions of the re-gate blocks and the push block.

    Also polices the structural assumption itself, so a SKILL.md reshape that
    adds or drops one of these blocks fails loudly here rather than silently
    changing what the ordering pin means.
    """
    regate_indices = [i for i, b in enumerate(blocks) if _REGATE in b]
    push_indices = [i for i, b in enumerate(blocks) if _PUSH in b]
    assert len(regate_indices) == 2, (
        f"expected exactly 2 fenced blocks running `{_REGATE}` (Section 3's "
        f"Green re-gate + the Red isolation-replay re-gate), found "
        f"{len(regate_indices)} -- this test's assumption about SKILL.md's "
        "structure has drifted; re-check by hand before adjusting the count"
    )
    assert len(push_indices) == 1, (
        f"expected exactly 1 fenced block running `{_PUSH}`, found "
        f"{len(push_indices)} -- this test's assumption about SKILL.md's "
        "structure has drifted; re-check by hand before adjusting the count"
    )
    return regate_indices, push_indices[0]


def _regate_precedes_push(blocks: list[str]) -> bool:
    """Whether EVERY re-gate block precedes the push block.

    Shared by the pin below and its sabotage twin ON PURPOSE: a twin that
    re-derives the comparison instead of calling it proves only that the
    re-derived expression is order-sensitive, which says nothing about the
    assertion actually shipped. Routing both through this one function is what
    makes the sabotage bind -- weaken the comparison here and the twin goes red.
    """
    regate_indices, push_index = _regate_and_push_indices(blocks)
    return max(regate_indices) < push_index


def test_section_3_regate_precedes_section_4_push_origin_trunk() -> None:
    """lode-youi: pins mechanically what lode-rlz8 left to prose -- Section 4's
    `origin/trunk` push sits after Section 3's re-gate, in document order.

    A DOCUMENT-ORDER pin, not an execution-order guarantee: an agent that skips
    a section could still push un-gated content even with this test green (the
    same insufficiency `test_assert_main_checkout.py`'s `_assert_guard_precedes`
    docstring raises for its own, narrower, intra-block pin). It earns its keep
    anyway because the threat Section 4's prose names is exactly a document edit
    ("if a future edit ever reorders push and gate"), and document order is
    precisely what such an edit changes.

    Owned here as a property of `/land`'s own Section 3 -> Section 4
    sequencing, not as a service to `scripts/recycled-worktree-guard.sh`: the
    premise ("`origin/trunk` only ever advances to already-gated content") is
    relied on by every launch worktree, which branches from `origin/trunk`
    (`.claude/settings.json`'s `worktree.baseRef: "fresh"`) -- so a reorder's
    blast radius is every fresh agent worktree, not just that guard's reset
    path.
    """
    blocks = _skill_blocks()
    regate_indices, push_index = _regate_and_push_indices(blocks)
    assert _regate_precedes_push(blocks), (
        f"Section 3's re-gate (`{_REGATE}`) no longer precedes Section 4's "
        f"`{_PUSH}` in document order -- re-gate blocks at {regate_indices}, "
        f"push block at {push_index}. A reorder here would let un-gated "
        "content reach `origin/trunk`, and every fresh agent worktree branches "
        "from it (lode-youi, lode-rlz8)"
    )


def test_section_3_regate_precedes_push_is_sabotage_proven() -> None:
    """Proves the pin above is non-vacuous by running the SAME
    `_regate_precedes_push()` it asserts on, against a minimally reordered copy
    of the real parsed block list.

    The sabotage is the exact edit the pin exists to catch: swap the push block
    with the LAST re-gate block, i.e. hoist the push above Section 3's re-gate.
    Deliberately not "move the push to index 0" -- that makes the comparison
    `max(regate) < 0`, false for ANY input, so such a twin passes even against a
    document that is already broken, and proves nothing.
    """
    blocks = _skill_blocks()
    regate_indices, push_index = _regate_and_push_indices(blocks)

    sabotaged = list(blocks)
    last_regate = max(regate_indices)
    sabotaged[push_index], sabotaged[last_regate] = (
        sabotaged[last_regate],
        sabotaged[push_index],
    )
    assert not _regate_precedes_push(sabotaged), (
        "sabotage (hoisting the push above Section 3's re-gate) did not make "
        "`_regate_precedes_push()` return False -- the real pin is vacuous"
    )

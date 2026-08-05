"""Guard-coverage gates for `.claude/skills/land/SKILL.md`'s use of
`scripts/assert-main-checkout.sh` (lode-pcee, lode-1d2y).

These tests run no script and touch no repo. They parse the SHIPPED
`.claude/skills/land/SKILL.md` and assert over its fenced ```bash blocks'
text -- because the defect this guard exists to prevent lives in a markdown
fence that no other gate parses. `scripts/assert-main-checkout.sh`'s own
runtime behaviour (real script, real git repos) is covered separately in
`tests/test_assert_main_checkout.py`; this module is the text-gate half that
split out of it (lode-2thl).
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import _fenced_bash, bash_fence_blocks

# Share lode-x495's quote-aware comment stripper rather than adding a second,
# competing implementation -- the same reuse `tests/test_bd_list_limit_gate.py`
# was told to make, for the same reason: "what does an agent actually execute"
# parsing is identical across these gates even when the assertions differ.
from test_skill_bash_state import _strip_comment

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Call-site pin against the SHIPPED SKILL.md (the fence is where the bug was)
# ---------------------------------------------------------------------------

LAND_SKILL = REPO_ROOT / ".claude" / "skills" / "land" / "SKILL.md"


# This module carried the last private copy of the ```bash fence parser until
# lode-p4qb
# folded it into `tests/conftest.py::bash_fence_blocks`. Why the four copies
# existed and what the shared parser's rules now are live next to the parser
# itself -- deliberately not restated here, per the same no-second-copy rule
# tests/test_land_lock.py and tests/test_land_conflicts_state.py follow.


_GUARD = "scripts/assert-main-checkout.sh"


def test_land_skill_never_reintroduces_the_false_dash_c_idiom() -> None:
    """The exact regression this ticket fixes: `-C "$(git rev-parse
    --show-toplevel)"` gives the *appearance* of pinning a command to the
    main checkout while actually just restating cwd. Must never reappear in
    an EXECUTED fence, and the destructive `git reset --hard origin/trunk`
    line must never carry any `-C` either (that would be the same false
    assurance one line later)."""
    executed = _fenced_bash(LAND_SKILL.read_text(encoding="utf-8"))

    assert "show-toplevel" not in executed, (
        "land/SKILL.md executes a `--show-toplevel`-derived -C again -- this "
        "is the false-assurance idiom lode-pcee removed; it cannot distinguish "
        "the main checkout from a worktree because it resolves relative to cwd"
    )

    for line in executed.splitlines():
        if "git reset --hard origin/trunk" in line:
            assert " -C " not in line and not line.strip().startswith("-C "), (
                f"the destructive reset carries a -C again: {line!r} -- this is "
                "the exact unguarded, most dangerous half of the lode-pcee defect"
            )


# ---------------------------------------------------------------------------
# Guard-coverage SWEEP (lode-1d2y; superseded the four per-fence pins in
# lode-8p3c) -- open-world: catches a NEW unguarded fence, not only the
# already-known ones. `_unguarded_mutations` owns its mechanics.
#
# Until lode-8p3c this module also pinned each already-discovered guarded fence
# individually, anchored on the commands it contained. That was precise, but
# closed-world: a genuinely NEW class of unguarded mutation -- `git clean -fdx`,
# `git rebase`, a `git checkout -f` with no accompanying reset, a bare `git
# commit -m` missing `--no-verify` -- matched none of those hand-picked
# selectors and was caught by NOTHING. That is exactly how lode-pxyt's own two
# exposures survived a full ticket cycle before a human noticed them by
# inspection.
#
# Subsumption was NOT free, and was not true as first written. Measured during
# lode-8p3c's technical review by replaying each deleted pin's sabotages against
# this sweep: the Section 1 pin also protected `bd dolt pull` and `git fetch
# origin`, and hoisting either into an unguarded fence of its own left the sweep
# GREEN while the deleted pin went RED. Both were added to `_MUTATING_CMD_RE` at
# zero allowlist cost, since the file's only occurrences of each were already
# guarded. Before deleting any future pin, diff its protected set against that
# pattern the same way.
#
# KNOWN LIMITATIONS (lode-1d2y):
#
# 1. INLINE COMMANDS ONLY -- SCRIPT-REFERENCE FOLLOWING IS DELIBERATELY OUT
#    OF SCOPE. This sweep does not open `scripts/*.sh` and classify what it
#    mutates; it only sees commands written directly in the fence. It would
#    NOT have caught lode-pxyt's first exposure on its own merits: Section
#    3's first-pass merge loop contains zero bare mutating git commands --
#    its only mutation is inside `scripts/land-merge-one.sh`, named literally
#    in `_MUTATING_CMD_RE`. A brand-new script reference is caught by nothing
#    here and would need the same manual discovery lode-pxyt's did. Measured
#    while writing this: of the scripts referenced from an UNGUARDED fence
#    today, none runs a cwd-resolved mutating git command -- their only git
#    calls are `merge-base --is-ancestor` and `merge-tree --write-tree`, both
#    read-only -- so the gap is latent, not live. Re-measure rather than
#    assume.
# 2. THE MUTATING-COMMAND REGEX IS DELIBERATELY OVER-BROAD, BY DESIGN. It
#    matches `git branch`, `git worktree`, and `git merge` as whole verbs,
#    not just the destructive subcommands (`branch -D`, `worktree remove`,
#    a bare `merge`) -- so it also flags read-only lookalikes: `git merge-
#    base` (`\b` matches against the hyphen), `git branch --merged`, `git
#    worktree list`. Narrowing the regex to exclude every read-only shape
#    would inherit the risk of excluding a genuinely dangerous one by the
#    same narrowing, so each false positive gets an allowlist entry instead.
# 3. AN ALLOWLIST ENTRY GOES STALE IN ONE DIRECTION SILENTLY, AND THAT
#    DIRECTION IS NOT COVERED. A change to an exempt line's TEXT re-flags it
#    loudly (`test_sweep_allowlist_match_is_exact_text_not_shape`), and a
#    deleted line fails `test_every_allowlist_entry_still_matches_a_real_
#    command`. A change to an exempt line's SURROUNDINGS does neither: four
#    reasons below are claims about variable PROVENANCE (`$WT`/`$BR` "read
#    from `git worktree list --porcelain`") that nothing here checks, so
#    rewriting those assignments leaves the key matching byte-for-byte with
#    its recorded reason now false.
# 4. BLOCK EXTRACTION INHERITS `tests/conftest.py::bash_fence_blocks`'s
#    DOCUMENTED BOUNDARIES -- the one failure mode an open-world sweep must
#    not have quietly. lode-p4qb answered the three that used to yield NO
#    BLOCK silently (an unterminated final fence is now flushed; four-backtick
#    and tilde fences are now scanned), so what remains is one boundary of the
#    opposite kind: a content line whose first non-blank character is `>` is
#    silently CORRUPTED by the blockquote strip, not skipped. A mutating
#    command written that way would reach this sweep in mangled form. That
#    helper owns the rule and the measurement; do not restate either here.
# ---------------------------------------------------------------------------

# Every command shape that mutates cwd's repo. Not git-only, deliberately:
# `bd dolt pull` writes cwd's OWN `.beads/` Dolt DB (each worktree carries its
# own copy), so a wrong-directory run updates the wrong database, and
# `scripts/land-merge-one.sh` reaches a bare `git merge --no-ff` the same way.
# The sweep asks ONE question -- does this line mutate cwd's repo -- so all
# three classes belong in one pattern rather than a git list plus side checks.
#
# The bd WRITE side is out of scope here, not overlooked: `land/SKILL.md` only
# ever reaches it through `scripts/bd-dolt-push.sh`, which is KNOWN
# LIMITATION 1's territory (no script following), not this pattern's.
_MUTATING_CMD_RE = re.compile(
    r"\b(?:git\s+(?:add|am|apply|branch|checkout|cherry-pick|clean|commit|fetch"
    r"|merge|mv|pull|push|rebase|reset|restore|revert|rm|stash|switch|worktree)"
    r"|bd\s+dolt\s+pull"
    r"|scripts/land-merge-one\.sh)\b"
)

# Exact command text (comment-stripped, `.strip()`'d) -> why it needs no
# `scripts/assert-main-checkout.sh` guard. Keying on exact text is what makes
# an exemption reviewable data rather than prose; KNOWN LIMITATION 3 above
# records what that keying does and does not catch.
_KNOWN_LAND_SKILL_MUTATIONS: dict[str, str] = {
    # --- read-only false positives of the broad verb regex (limitation 2) ---
    'for mb in $(git merge-base --all "origin/land/<X>" "origin/land/<Y>"); do': (
        "read-only: enumerates merge-bases, mutates nothing (1a stacked-branch "
        "detection)"
    ),
    'git merge-base --is-ancestor "$mb" origin/trunk || OFF_TRUNK="$OFF_TRUNK $mb"': (
        "read-only: --is-ancestor is a pure query, mutates nothing"
    ),
    'LOCK_REASON=$(git worktree list --porcelain | awk -v want="$WT" \'': (
        "read-only listing (Section 4 worktree-GC loop's stale-lock reason lookup)"
    ),
    "done < <(git worktree list --porcelain | awk '": (
        "read-only listing (Section 4 worktree-GC loop's candidate enumeration)"
    ),
    "MERGED=$(git branch --merged trunk --format='%(refname:short)')": (
        "read-only listing (Section 4's third backstop, computing the merged set)"
    ),
    "CHECKED_OUT=$(git worktree list --porcelain | awk "
    "'/^branch refs\\/heads\\//{print substr($0,19)}')": (
        "read-only listing (Section 4's third backstop, computing checked-out branches)"
    ),
    # --- explicit ref/path-addressed writes: cwd-independent by construction,
    #     the same reasoning Section 4's own prose gives for these commands
    #     ("each names its own target") ---
    "git push origin trunk": (
        "ref-addressed (explicit remote+branch); Section 4's own text: cwd-independent"
    ),
    'git push origin --delete "land/$id"': (
        "ref-addressed delete (explicit remote+branch); cwd-independent "
        "(Section 4's per-ticket branch GC)"
    ),
    'git push origin --delete "land/<id>"': (
        "ref-addressed delete (explicit remote+branch); cwd-independent "
        "(Bounce / Escalate-exit-(b) / Escalate-exit-(c))"
    ),
    'git worktree unlock "$WT" 2>/dev/null || true': (
        "path-addressed to $WT (the exact worktree path just read from "
        "`git worktree list --porcelain`), not cwd-resolved"
    ),
    'if git worktree remove --force "$WT"; then': (
        "path-addressed to $WT, not cwd-resolved (same reasoning as worktree "
        "unlock above)"
    ),
    '[ -n "$BR" ] && git branch -D "$BR" 2>/dev/null || true': (
        "ref-addressed to $BR, an explicit branch name read from the same "
        "porcelain listing, not cwd-resolved"
    ),
    'if git branch -D "$BR" 2>/dev/null; then': (
        "ref-addressed to $BR, an explicit branch name, not cwd-resolved"
    ),
    "git worktree prune": (
        "housekeeping only: drops stale worktree admin entries already gone "
        "from disk; never removes real content or a live worktree"
    ),
    # --- path-addressed to the passive bd export, never real work ---
    "git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true": (
        "path-addressed to the passive .beads/issues.jsonl export only -- "
        "never real work (import.auto: false, lode-6ra); a wrong-directory "
        "run only restores that worktree's own copy of a regenerated artifact"
    ),
}


def _unguarded_mutations(markdown: str, *, allowlist: dict[str, str]) -> list[str]:
    """Every fenced ```bash block's mutating command that is neither
    allowlisted nor preceded, in its OWN block, by `scripts/assert-main-
    checkout.sh`. Empty means full coverage.

    Block boundaries are load-bearing, not tidiness: per land/SKILL.md's
    governing rule (lode-sfnb) each fence is its own Bash invocation, so a
    guard only protects commands in the SAME fence. `guarded` therefore resets
    per block, and reading it in document order is the whole of the "precedes"
    check -- no positional arithmetic, which would go ambiguous the moment two
    lines in one block carry identical text. See the module comment above for
    the regex/allowlist design and its known limitations.
    """
    violations: list[str] = []
    for block_index, block in enumerate(bash_fence_blocks(markdown)):
        guarded = False
        for raw_line in block.splitlines():
            cmd = _strip_comment(raw_line).strip()
            if not cmd:
                continue
            if _GUARD in cmd:
                guarded = True
                continue
            if not _MUTATING_CMD_RE.search(cmd):
                continue
            if guarded or cmd in allowlist:
                continue
            violations.append(
                f"block {block_index}: {cmd!r} is a mutating command with "
                f"no preceding {_GUARD} in its own fenced block, and is not "
                "in the allowlist -- either guard it or record a reasoned "
                "allowlist entry"
            )
    return violations


def test_land_skill_guard_covers_every_known_mutating_fence() -> None:
    """The sweep, run against the REAL land/SKILL.md with the real allowlist.
    Zero violations means every mutating command in the file is either
    guarded or a recorded, reasoned exemption -- across every fence in the
    file, not just a hand-picked few.

    This also subsumes the Section 1 existence pin lode-pcee added and
    lode-0mkv deleted ("does SKILL.md call the guard at all"): measured,
    dropping Section 1's guard line leaves that pin GREEN -- the string still
    appears in three other fences -- while this sweep reports 4 named
    violations. Do not re-add a per-section duplicate.
    """
    violations = _unguarded_mutations(
        LAND_SKILL.read_text(encoding="utf-8"),
        allowlist=_KNOWN_LAND_SKILL_MUTATIONS,
    )
    assert violations == [], "\n".join(violations)


def test_every_allowlist_entry_still_matches_a_real_command() -> None:
    """The sweep above passes when it finds no violation -- including when it
    finds NOTHING AT ALL. Two silent regressions produce that: a fence parser
    that stops recognizing blocks (the lode-ovgs class), and an exempt command
    deleted from land/SKILL.md while its entry lives on as a frozen verdict
    over text that no longer exists. Both leave `_KNOWN_LAND_SKILL_MUTATIONS`
    entries matching nothing, so requiring every entry to still hit a real
    line is what makes the green above mean something.
    """
    blocks = bash_fence_blocks(LAND_SKILL.read_text(encoding="utf-8"))
    live = {
        _strip_comment(raw).strip() for block in blocks for raw in block.splitlines()
    }

    orphaned = sorted(set(_KNOWN_LAND_SKILL_MUTATIONS) - live)

    assert orphaned == [], (
        "these allowlist entries no longer match any command in land/SKILL.md, "
        "so they exempt nothing and the sweep's green is that much emptier -- "
        f"delete them or re-derive them from the current file: {orphaned}"
    )


def test_sweep_catches_a_brand_new_unguarded_mutation() -> None:
    """The property this whole ticket exists to add: a mutating fence with NO
    guard at all, and not present in any allowlist, must be caught.

    Parametrized over one representative command per mutating verb the regex
    is meant to cover, because the verb list is the sweep's entire open-world
    boundary: dropping a verb from it silently un-covers that whole class
    while every other test here stays green. `git pull` and `git add` are in
    the list by measurement, not taste -- an earlier draft of this sweep
    omitted both, and `git add` is the omission lode-pxyt's review had already
    had to fix once in the per-fence pin lode-8p3c has since deleted.
    """
    for command in (
        "git add .",
        "git am /tmp/p.patch",
        "git apply /tmp/p.patch",
        "git checkout -f trunk",
        "git cherry-pick deadbeef",
        "git clean -fdx",
        "git commit -m wip",
        "git fetch origin",
        "git merge --no-ff origin/land/x",
        "git pull --rebase",
        "git rebase origin/trunk",
        "git reset --hard HEAD~1",
        "git restore --worktree .",
        "git revert HEAD",
        "git stash pop",
        "git switch trunk",
        # Not git, but a cwd-resolved mutation the sweep must still see -- see
        # `_MUTATING_CMD_RE`'s non-git alternatives (lode-8p3c).
        "bd dolt pull",
    ):
        markdown = f"1. Some new step:\n\n   ```bash\n   {command}\n   ```\n"

        violations = _unguarded_mutations(markdown, allowlist={})

        assert violations, (
            f"an unguarded, unallowlisted {command!r} was not flagged -- the "
            "mutating-verb regex no longer covers that verb, and nothing else "
            "in this module would notice"
        )
        assert command in violations[0]


def test_sweep_requires_the_guard_precede_not_merely_be_present() -> None:
    """A guard call present in the block but AFTER the mutating command does
    not protect it -- ordering matters."""
    markdown = f"```bash\ngit reset --hard origin/trunk\n{_GUARD} || exit 1\n```\n"
    violations = _unguarded_mutations(markdown, allowlist={})
    assert violations, "a guard AFTER the mutation must not count as coverage"


def test_sweep_allowlist_match_is_exact_text_not_shape() -> None:
    """Per this ticket's acceptance criteria: the sweep must fail if an
    allowlisted command's text changes shape, even cosmetically -- proving
    the allowlist is keyed on exact text, not on some looser notion of "the
    same command". Take a real allowlisted entry and perturb it slightly (an
    added flag); the perturbed text is a different string, so it no longer
    matches the allowlist key and must be re-flagged as an unguarded
    candidate needing its own guard or its own fresh allowlist entry.

    Both halves are asserted. The perturbed half alone would pass even if the
    allowlist were never consulted at all, so the unperturbed half is what
    proves the exemption is real and the flag is caused by the perturbation.
    """
    original = "git push origin trunk"
    perturbed = original + " --porcelain"

    assert (
        _unguarded_mutations(
            f"```bash\n{original}\n```\n", allowlist=_KNOWN_LAND_SKILL_MUTATIONS
        )
        == []
    ), "the unperturbed command is not actually exempt -- fixture assumption broken"

    assert _unguarded_mutations(
        f"```bash\n{perturbed}\n```\n", allowlist=_KNOWN_LAND_SKILL_MUTATIONS
    ), (
        "a cosmetically-changed allowlisted command must not silently keep "
        "matching its old allowlist entry"
    )

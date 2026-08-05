"""Tests for scripts/assert-main-checkout.sh (lode-pcee).

`/land`'s Section 1 used to run its `checkout -f trunk` through
`git -C "$(git rev-parse --show-toplevel)"`, on the theory that the `-C`
pinned the command to lode's main checkout. It did not: `--show-toplevel`
resolves relative to CWD, so the value it produces is always wherever the
process already is -- in the main checkout that made the `-C` a no-op
(redundant, not wrong); in a linked worktree it resolved to THAT WORKTREE's
own root, never the main checkout, because a `-C` computed from cwd cannot
redirect a command to a *different* directory than the one it is already
running in. That reads as a safety guard and is not one. The genuinely
destructive line two lines later -- `git reset --hard origin/trunk` --
carried no `-C` at all, so run from a worktree it would hard-reset THAT
WORKTREE's own branch, destroying any uncommitted work there with nothing in
`git reflog` to recover it (discarded commits are recoverable; discarded
uncommitted work is not). `/land` is defined to run only in the main
checkout, so this was latent, not live -- but a guard that looks like
protection and provides none is worse than no guard.

This script replaces the `-C` idiom with an IDENTITY check:
`git rev-parse --git-common-dir` returns the one `.git` directory every
worktree of a repo shares, main checkout included, so the main checkout's
own toplevel is that directory's parent and a linked worktree's toplevel
never is. Unlike `--show-toplevel`, that value does not depend on which
worktree the process happens to be standing in, which is what makes it
usable to DISTINGUISH the two rather than just restate wherever cwd already
is.

The SCRIPT-BEHAVIOUR tests below run the ACTUAL
`scripts/assert-main-checkout.sh` against real git repositories (including
real `git worktree add` checkouts and a real submodule) built in `tmp_path` --
no fake git, no mocked subprocess -- sabotage-provable per the lode-verb bar:
reverting the script back to comparing `--show-toplevel` against itself would
turn the worktree-refusal test below green for the wrong reason, but the
submodule/layout test would catch a script that assumes `--git-common-dir`
always ends in `/.git`.

From "Call-site pin against the SHIPPED SKILL.md" onward the subject changes:
those tests run no script and touch no repo. They assert against the text of
`.claude/skills/land/SKILL.md` itself, because the defect this guard exists to
prevent lives in a markdown fence that no other gate parses.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _gitrepo import _git
from conftest import LAND_SKILL, _fenced_bash, bash_fence_blocks

# Share lode-x495's quote-aware comment stripper rather than adding a second,
# competing implementation -- the same reuse `tests/test_bd_list_limit_gate.py`
# was told to make, for the same reason: "what does an agent actually execute"
# parsing is identical across these gates even when the assertions differ.
from test_skill_bash_state import _strip_comment

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "assert-main-checkout.sh"


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A throwaway repo with one commit on `trunk`, isolated user config."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _add_worktree(repo: Path, rel_path: str, branch: str) -> Path:
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, "trunk")
    return wt


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_wrong_argument_count_exits_2(tmp_path: Path) -> None:
    """Any argument at all is a usage error (exit 2) -- this is a pure,
    unparametrized precondition, distinct from a location verdict (0/1)."""
    repo = _init_repo(tmp_path)
    result = _run(repo, "unexpected-arg")
    assert result.returncode == 2, result.stdout + result.stderr


def test_main_checkout_passes(tmp_path: Path) -> None:
    """The genuine case: cwd is the main checkout's own toplevel -- exit 0,
    nothing printed to stderr."""
    repo = _init_repo(tmp_path)

    result = _run(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_subdirectory_of_main_checkout_passes(tmp_path: Path) -> None:
    """`--show-toplevel` walks up to the repo root regardless of which
    subdirectory cwd is in, so this must still resolve to the main checkout
    and pass -- the assertion is about the REPO, not the exact cwd string."""
    repo = _init_repo(tmp_path)
    subdir = repo / "scripts"
    subdir.mkdir()

    result = _run(subdir)

    assert result.returncode == 0, result.stdout + result.stderr


def test_linked_worktree_is_refused(tmp_path: Path) -> None:
    """The exact lode-pcee scenario: cwd is a linked worktree, not the main
    checkout. Must refuse (exit 1) and name lode-pcee plus a hard-stop
    instruction in its diagnostic -- and must NOT claim this is the main
    checkout the way the old `-C "$(git rev-parse --show-toplevel)"` idiom
    effectively did (that value, read from inside this same worktree, IS this
    worktree's own root -- proving the old idiom never redirected anywhere)."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, "feature-wt", "feature")

    old_idiom_value = _git(wt, "rev-parse", "--show-toplevel").stdout.strip()
    assert old_idiom_value == str(wt), (
        "sanity check on the bug itself: --show-toplevel from inside the "
        "worktree must resolve to the WORKTREE, proving `-C` computed from "
        "it can never redirect a command to the main checkout"
    )

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "lode-pcee" in result.stderr
    assert "NOT RUNNING IN THE MAIN CHECKOUT" in result.stderr
    assert "STOP AND REPORT" in result.stderr
    assert str(wt) in result.stderr
    assert str(repo) in result.stderr


def test_subdirectory_of_a_worktree_is_also_refused(tmp_path: Path) -> None:
    """Same as the subdirectory-passes case above, mirrored on the refusal
    side: a subdirectory of a linked worktree must still be refused, not
    accidentally treated as ambiguous."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, "feature-wt", "feature")
    subdir = wt / "scripts"
    subdir.mkdir()

    result = _run(subdir)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT RUNNING IN THE MAIN CHECKOUT" in result.stderr


def test_unsupported_layout_is_a_machine_fault_not_a_location_verdict(
    tmp_path: Path,
) -> None:
    """A real `git submodule` checkout's `--git-common-dir` points at
    `<super>/.git/modules/<name>`, which does NOT end in `/.git` -- the one
    assumption the main-checkout derivation relies on. This must be
    distinguished from "wrong directory" (exit 1): it is exit 2, a machine
    fault / unsupported layout, so a caller does not misreport it as a
    worktree contamination problem."""
    outer = _init_repo(tmp_path, name="outer")
    inner = _init_repo(tmp_path, name="inner")

    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(inner),
            "sub",
        ],
        cwd=outer,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    sub = outer / "sub"

    common_dir = _git(
        sub, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).stdout.strip()
    assert not common_dir.endswith("/.git"), (
        "sanity check on the fixture itself: a submodule's --git-common-dir "
        "must NOT end in /.git, or this test isn't exercising the fallback"
    )

    result = _run(sub)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "unsupported repository layout" in result.stderr
    assert "MACHINE FAULT" in result.stderr


def test_not_inside_any_repository_is_exit_2_not_a_raw_git_128(
    tmp_path: Path,
) -> None:
    """cwd outside any git repository at all -- `git rev-parse` fails, and the
    script must convert that into its own documented exit 2 with a lode-pcee
    diagnostic, NOT let `set -e` propagate git's raw 128.

    This is the same class of harness misdispatch that motivated
    `scripts/isolation-guard.sh` (lode-ska2), so it is reachable, not
    hypothetical. 128 is outside the 0/1/2 contract the header promises, and
    a caller that only distinguishes those three cannot tell it apart from a
    location verdict -- exactly the machine-vs-content confusion lode-9i2p's
    exit-2 convention exists to prevent.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run(outside)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr


def test_inside_the_git_dir_is_a_machine_fault_not_a_location_verdict(
    tmp_path: Path,
) -> None:
    """The second wrapped `git rev-parse` -- the `--show-toplevel` one -- has
    its own exit-2 path, and this is what reaches it: with cwd inside `.git/`,
    `--git-common-dir` still answers happily but there is NO work tree, so
    `--show-toplevel` fails with git's raw 128.

    Reached only through that ordering, so it is not covered by the
    not-inside-any-repository test above (which fails at the FIRST rev-parse).
    Without this, the branch that converts 128 into the documented exit 2 is
    the one arm of the 0/1/2 contract with no test at all -- and a regression
    there would leak an undocumented status that a caller cannot distinguish
    from a location verdict."""
    repo = _init_repo(tmp_path)

    result = _run(repo / ".git")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    # Specifically the show-toplevel arm, not the --git-common-dir one.
    assert "--show-toplevel' failed" in result.stderr


def test_refusal_never_mutates_anything(tmp_path: Path) -> None:
    """This script only asserts; it never redirects or repairs. Confirm HEAD,
    branches, and the working tree are untouched on a refusal."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, "feature-wt", "feature")
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()
    (wt / "untracked.txt").write_text("must survive\n")

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (wt / "untracked.txt").exists()


# ---------------------------------------------------------------------------
# Call-site pin against the SHIPPED SKILL.md (the fence is where the bug was)
# ---------------------------------------------------------------------------

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
#    NOT have caught lode-pxyt's exposure: that fence contained zero bare
#    mutating git commands -- its only mutation was inside a referenced
#    script. A script reference is caught by nothing here and would need the
#    same manual discovery lode-pxyt's did. Measured while writing this, and
#    re-measured for lode-1nty: of the scripts referenced from an UNGUARDED
#    fence today, the only one running a cwd-resolved mutating git command is
#    `scripts/land-merge-one.sh`, which asserts its OWN main-checkout identity
#    internally (see that script's header) rather than depending on this
#    sweep; every other one's git calls are `merge-base --is-ancestor` and
#    `merge-tree --write-tree`, both read-only. So the gap is latent, not
#    live. Re-measure rather than assume.
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
#
# `test_every_allowlist_entry_still_matches_a_real_command` below is a hand-written
# liveness pin, mirroring the precedent `test_skill_bash_state.py::_dead_allowlist_keys`
# set. Whether these hand-written pins should share one mechanism was decided
# (rejected extraction, in favor of shared discipline instead) in
# docs/decisions.md, search "The three hand-written liveness pins stay separate"
# (lode-7zap). Do not re-litigate here.
#
# DECISION (lode-eu04): `_dead_allowlist_entries`' live set also ignored
# `_unguarded_mutations`' BLOCK-level guard state (a key reachable only inside
# an already-guarded block, or only on the `_GUARD`-carrying line itself, read
# as live while excusing nothing). Two options were on the table:
#   (a) thread the block-level guard state into a SECOND pass tailored to
#       `_dead_allowlist_entries`, mirroring `_unguarded_mutations`' loop; or
#   (b) factor `_unguarded_mutations`' own loop into a shared primitive
#       (`_unguarded_candidates`) returning every unguarded, mutating command
#       regardless of allowlist, and define "live" as membership in that set --
#       i.e. what the sweep would flag with an emptied allowlist.
# Chose (b). Rejected (a) because it is a second, independently-maintained
# copy of the exact block-state machine `_unguarded_mutations` already owns --
# two loops that must be kept in lockstep by hand is exactly how this class of
# gap (lode-dkak, now this ticket) keeps recurring one level at a time. (b)
# makes drift structurally impossible: `_dead_allowlist_entries` and
# `_unguarded_mutations` now both read the SAME single candidate list, so
# "live" cannot silently diverge from what the sweep actually excuses again.
# ---------------------------------------------------------------------------

# Every command shape that mutates cwd's repo. Not git-only, deliberately:
# `bd dolt pull` writes cwd's OWN `.beads/` Dolt DB (each worktree carries its
# own copy), so a wrong-directory run updates the wrong database. The sweep
# asks ONE question -- does this line mutate cwd's repo -- so both classes
# belong in one pattern rather than a git list plus side checks.
#
# Do NOT re-add `scripts/land-merge-one.sh` here: it was named literally as a
# special case until lode-1nty, and now asserts its own main-checkout identity
# internally instead, so this sweep does not need to know it exists. Re-adding
# it means re-litigating that decision (docs/agents-workflow.md's main-checkout
# section).
#
# The bd WRITE side is out of scope here, not overlooked: `land/SKILL.md` only
# ever reaches it through `scripts/bd-dolt-push.sh`, which is KNOWN
# LIMITATION 1's territory (no script following), not this pattern's.
_MUTATING_CMD_RE = re.compile(
    r"\b(?:git\s+(?:add|am|apply|branch|checkout|cherry-pick|clean|commit|fetch"
    r"|merge|mv|pull|push|rebase|reset|restore|revert|rm|stash|switch|worktree)"
    r"|bd\s+dolt\s+pull)\b"
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


def _normalized_line(raw_line: str) -> str:
    """A raw fenced-block line reduced to its command text: comment-stripped
    and `.strip()`'d. Empty means the line carries no command at all.
    """
    return _strip_comment(raw_line).strip()


def _is_mutating(cmd: str) -> bool:
    """Whether a `_normalized_line` result is a mutating command -- i.e.
    whether an allowlist entry keyed on this exact text could excuse
    anything.
    """
    return bool(cmd) and bool(_MUTATING_CMD_RE.search(cmd))


def _unguarded_candidates(markdown: str) -> list[tuple[int, str]]:
    """Every fenced ```bash block's mutating command that is not preceded, in
    its OWN block, by `scripts/assert-main-checkout.sh` -- i.e. exactly the
    set `_unguarded_mutations` would flag as a violation if `allowlist` were
    empty, paired with its block index. `_unguarded_mutations` and
    `_dead_allowlist_entries` both build on this, and this list is the single
    definition of "live" both mean: a key only excuses anything in the sweep
    if it names a command reachable at THIS point (unguarded position,
    mutating verb) -- not merely present anywhere in the file. See the
    lode-eu04 DECISION in the module comment above for why the two callers
    share one list rather than each owning a loop.

    Block boundaries are load-bearing, not tidiness: per land/SKILL.md's
    governing rule (lode-sfnb) each fence is its own Bash invocation, so a
    guard only protects commands in the SAME fence. `guarded` therefore resets
    per block, and reading it in document order is the whole of the "precedes"
    check -- no positional arithmetic, which would go ambiguous the moment two
    lines in one block carry identical text. See the module comment above for
    the regex/allowlist design and its known limitations.
    """
    candidates: list[tuple[int, str]] = []
    for block_index, block in enumerate(bash_fence_blocks(markdown)):
        guarded = False
        for raw_line in block.splitlines():
            cmd = _normalized_line(raw_line)
            if not cmd:
                continue
            if _GUARD in cmd:
                guarded = True
                continue
            if not _is_mutating(cmd):
                continue
            if guarded:
                continue
            candidates.append((block_index, cmd))
    return candidates


def _unguarded_mutations(markdown: str, *, allowlist: dict[str, str]) -> list[str]:
    """Every unguarded candidate (see `_unguarded_candidates`) not excused by
    `allowlist`. Empty means full coverage.
    """
    return [
        f"block {block_index}: {cmd!r} is a mutating command with "
        f"no preceding {_GUARD} in its own fenced block, and is not "
        "in the allowlist -- either guard it or record a reasoned "
        "allowlist entry"
        for block_index, cmd in _unguarded_candidates(markdown)
        if cmd not in allowlist
    ]


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


def _dead_allowlist_entries(markdown: str, *, allowlist: dict[str, str]) -> list[str]:
    """Allowlist keys in `allowlist` that would excuse nothing in the real sweep
    over `markdown` -- i.e. that do not name any `_unguarded_candidates(markdown)`
    entry, which is the one definition of "live" this module has (see that
    helper, and the lode-dkak/lode-eu04 history in the module comment above for
    the two looser definitions it replaced). A key returned here is dead: it
    currently excuses nothing, but stays in the allowlist regardless, ready to
    silently re-excuse a brand-new command that happens to share its exact text.
    Argument order deliberately matches `_unguarded_mutations` above -- same two
    inputs, same shape.

    The parameterized-helper-plus-sabotage shape here is lode-e49j's, whose
    `test_skill_bash_state.py::_dead_allowlist_keys` modeled its own pin on THIS
    module's (lode-1d2y) and then shipped the non-vacuity half this module lacked;
    lode-7zap brings that half back the other way.

    `allowlist` and `markdown` are parameters, not read from the module globals
    directly, so `test_every_allowlist_entry_is_provably_checked_by_sabotage` below
    can exercise this exact primitive -- the same one the real pin calls -- against
    a synthetic fixture, without mutating the real `land/SKILL.md` on disk.
    """
    live = {cmd for _, cmd in _unguarded_candidates(markdown)}
    return sorted(set(allowlist) - live)


def test_every_allowlist_entry_still_matches_a_real_command() -> None:
    """The sweep above passes when it finds no violation -- including when it
    finds NOTHING AT ALL. Two silent regressions produce that: a fence parser
    that stops recognizing blocks (the lode-ovgs class), and an exempt command
    deleted from land/SKILL.md while its entry lives on as a frozen verdict
    over text that no longer exists. Both leave `_KNOWN_LAND_SKILL_MUTATIONS`
    entries matching nothing, so requiring every entry to still hit a real
    line is what makes the green above mean something.

    Non-vacuousness of THIS pin is proven separately, by
    `test_every_allowlist_entry_is_provably_checked_by_sabotage` below (lode-7zap) --
    a liveness pin is a negative assertion over data expected to be empty in the
    healthy case, so its interesting branch never runs on real data and this test
    alone cannot show it would catch a real regression.
    """
    orphaned = _dead_allowlist_entries(
        LAND_SKILL.read_text(encoding="utf-8"),
        allowlist=_KNOWN_LAND_SKILL_MUTATIONS,
    )

    assert orphaned == [], (
        "these allowlist entries no longer match any command in land/SKILL.md, "
        "so they exempt nothing and the sweep's green is that much emptier -- "
        f"delete them or re-derive them from the current file: {orphaned}"
    )


def test_every_allowlist_entry_is_provably_checked_by_sabotage() -> None:
    """Non-vacuousness proof for the pin above (lode-7zap): a test that passes both
    before and after the regression it's meant to catch is worthless. Mirrors the
    precedent this ticket exists to bring this module up to par with (lode-e49j),
    `test_skill_bash_state.py::test_every_allowlist_entry_is_provably_checked_by_sabotage`,
    including its measured fix: hold ONE key/fixture constant and
    vary only the markdown CONTENT between the live and dead assertions. Two
    differently-named fixtures would derive two different keys, so the "now dead"
    assertion would pass on a name mismatch alone and prove nothing about whether
    `_dead_allowlist_entries` actually tracks content -- the exact vacuous-sabotage
    shape lode-e49j's review caught and lode-1d2y's own pin (until now) had no
    counterpart to guard against at all.

    Sabotage recipe for this ticket's own future maintenance, recorded here per its
    acceptance criteria: replace the body of `_dead_allowlist_entries` with an
    unconditional `return []`, or delete any one of the three assertions below, and
    this test goes red. Both mutations were re-run independently at technical review
    (lode-7zap), as was a third -- a name-only helper, blind to content, which the
    LAST assertion is what catches. That last assertion is the one carrying the
    non-vacuity weight; the first two alone would not distinguish it.
    """
    key = "git push origin trunk"
    markdown_live = f"```bash\n{key}\n```\n"
    assert _dead_allowlist_entries(markdown_live, allowlist={key: "fixture"}) == [], (
        "fixture assumption broken: the key is not actually a live command in the "
        "unfixed markdown fixture"
    )

    bogus = "git push origin THIS_BRANCH_DOES_NOT_EXIST_ANYWHERE_lode_7zap"
    assert _dead_allowlist_entries(markdown_live, allowlist={bogus: "fixture"}) == [
        bogus
    ], "a bogus key matching no real command must be reported dead"

    markdown_fixed = f"```bash\n# {key}\n```\n"  # commented out -- no longer live
    assert _dead_allowlist_entries(markdown_fixed, allowlist={key: "fixture"}) == [
        key
    ], "removing the command from the corpus must flip the SAME key from live to dead"


def test_dead_allowlist_entries_requires_a_mutating_line_not_mere_presence() -> None:
    """Non-vacuity proof for lode-dkak's fix: before this ticket,
    `_dead_allowlist_entries` computed its live set as every comment-stripped,
    `.strip()`'d line in a fenced ```bash block -- with NO `_MUTATING_CMD_RE`
    filter, unlike `_unguarded_mutations`. So a key present in the corpus only as
    a line that never matches `_MUTATING_CMD_RE` (and therefore excuses nothing
    in the sweep -- `_unguarded_mutations` never even reaches its allowlist check
    for such a line) still read as LIVE and passed the liveness pin. That is
    precisely the dead-entry class the pin exists to catch, surviving the pin.

    Sabotage: hold ONE key constant, and use the exact PRE-FIX live-set
    computation (every comment-stripped line, unfiltered -- reproduced inline
    below, not imported, since the fixed `_dead_allowlist_entries` no longer
    computes it) to prove this fixture's key would have read as live before
    this ticket. Then run the SAME key/markdown through the fixed
    `_dead_allowlist_entries` and require it now report the key dead --
    proving the regex filter, not mere textual presence, is what "live" means.

    Recorded mutation (re-run it if you touch this): restore
    `_dead_allowlist_entries`' live set to the unfiltered comprehension
    reproduced below and run this module -- this test, and ONLY this test,
    must go red. Verified at review of lode-dkak: 1 failed, 16 passed.
    """
    key = "echo this line never matches _MUTATING_CMD_RE at all"
    markdown = f"```bash\n{key}\n```\n"

    assert not _MUTATING_CMD_RE.search(key), (
        "fixture assumption broken: the chosen text matches _MUTATING_CMD_RE "
        "after all, so it does not exercise the non-mutating-line case"
    )

    # Reproduce the PRE-FIX live-set computation (unfiltered, comment-stripped
    # lines only) to prove this exact key/markdown pair would have read as
    # live before lode-dkak -- i.e. that the sabotage is not vacuous.
    pre_fix_live = {
        _strip_comment(raw).strip()
        for block in bash_fence_blocks(markdown)
        for raw in block.splitlines()
    }
    assert key in pre_fix_live, (
        "fixture assumption broken: before lode-dkak's fix this key would not "
        "actually have read as live, so the sabotage proves nothing"
    )

    # The fix: the fixed `_dead_allowlist_entries` filters by `_is_mutating`,
    # the SAME primitive `_unguarded_mutations` uses, so a key present only
    # as a non-mutating
    # line excuses nothing and must be reported dead.
    assert _dead_allowlist_entries(markdown, allowlist={key: "fixture"}) == [key], (
        "a key present only as a non-mutating line must be reported dead -- "
        "mere textual presence does not make an allowlist entry live"
    )


def test_dead_allowlist_entries_requires_an_unguarded_position() -> None:
    """Non-vacuity proof for lode-eu04's fix: before it, the live set was every
    `_is_mutating` line in the corpus, blind to block-level guard state, so a
    key reachable only in an already-guarded position read as live.

    Sabotage recipe for this ticket's own future maintenance, per its
    acceptance criteria: hold ONE key constant and vary only the fixture's
    guard placement -- present before the mutating line (excuses nothing,
    must read dead) vs absent from the block entirely (the mutating line is
    reachable, must read live). The second dead case its acceptance criteria
    name -- a key that IS the `_GUARD`-carrying line, which sets `guarded` and
    `continue`s before the mutation check is ever reached -- is asserted last,
    with its own key (it cannot share one: the guard text is what makes it
    that case). Restoring the pre-fix live-set computation
    (every `_is_mutating` line, unfiltered by block guard state -- reproduced
    inline below, not imported, since the fixed `_dead_allowlist_entries` no
    longer computes it) must flip the guarded-block assertion from dead to
    live, proving the sabotage is not vacuous.
    """
    key = "git push origin trunk"

    # The block's ONLY mutating line is guarded -- `_unguarded_mutations`
    # never reaches its allowlist check here, so this key excuses nothing.
    markdown_guarded = f"```bash\n{_GUARD}\n{key}\n```\n"
    # Same key, same command text, but in a block with NO guard at all --
    # the mutating line is reachable and the key would excuse a real
    # violation.
    markdown_unguarded = f"```bash\n{key}\n```\n"

    # Reproduce the PRE-FIX live-set computation (every `_is_mutating` line,
    # blind to block guard state) to prove this exact key/fixture pair would
    # have read as live before lode-eu04 -- i.e. that the sabotage is not
    # vacuous.
    pre_fix_live = {
        cmd
        for block in bash_fence_blocks(markdown_guarded)
        for raw in block.splitlines()
        if _is_mutating(cmd := _normalized_line(raw))
    }
    assert key in pre_fix_live, (
        "fixture assumption broken: before lode-eu04's fix this key would not "
        "actually have read as live even in the guarded fixture, so the "
        "sabotage proves nothing"
    )

    # The fix: a key reachable only inside a guarded block excuses nothing
    # and must be reported dead.
    assert _dead_allowlist_entries(markdown_guarded, allowlist={key: "fixture"}) == [
        key
    ], (
        "a key present only inside an already-guarded block must be reported "
        "dead -- it excuses nothing in the real sweep"
    )

    # Same key, unguarded block: the SAME key must now read live, proving the
    # guarded-block assertion above is driven by guard placement, not by the
    # key's text.
    assert (
        _dead_allowlist_entries(markdown_unguarded, allowlist={key: "fixture"}) == []
    ), "the same key in an unguarded block must read live -- fixture assumption broken"

    # The other dead case in this ticket's acceptance criteria: a key whose own
    # text CARRIES the guard. Such a line sets `guarded` and `continue`s before
    # the mutation check, so it is never a candidate and excuses nothing --
    # even though it does match `_MUTATING_CMD_RE` on its own.
    guard_carrying = f"{_GUARD} && git push origin trunk"
    assert _is_mutating(guard_carrying), (
        "fixture assumption broken: the guard-carrying line must itself match "
        "_MUTATING_CMD_RE, or it exercises the lode-dkak case instead"
    )
    assert _dead_allowlist_entries(
        f"```bash\n{guard_carrying}\n```\n", allowlist={guard_carrying: "fixture"}
    ) == [guard_carrying], (
        "a key present only as the `_GUARD`-carrying line itself must be "
        "reported dead -- that line guards, it never gets excused"
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

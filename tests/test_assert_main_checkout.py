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
from conftest import bash_fence_blocks

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

LAND_SKILL = REPO_ROOT / ".claude" / "skills" / "land" / "SKILL.md"


# This module carried the last private copy of that parser until lode-p4qb
# folded it into `tests/conftest.py::bash_fence_blocks`. Why the four copies
# existed and what the shared parser's rules now are live next to the parser
# itself -- deliberately not restated here, per the same no-second-copy rule
# tests/test_land_lock.py and tests/test_land_conflicts_state.py follow.


def _fenced_bash(markdown: str) -> str:
    """The ```bash fences only, concatenated into one string -- what an agent
    actually EXECUTES.

    Scanning the whole file would match the prose that *explains* the old
    defect (it necessarily quotes the broken `-C "$(git rev-parse
    --show-toplevel)"` idiom), so the pin has to separate what is executed
    from what is merely described.
    """
    return "\n".join(bash_fence_blocks(markdown))


def test_land_skill_section1_calls_the_script() -> None:
    """Section 1 must actually invoke the guard, not just describe it in
    prose -- the defect this ticket fixes lived in a markdown fence, where no
    other gate reaches it."""
    executed = _fenced_bash(LAND_SKILL.read_text(encoding="utf-8"))
    assert "scripts/assert-main-checkout.sh" in executed, (
        "land/SKILL.md Section 1 never calls scripts/assert-main-checkout.sh -- "
        "the main-checkout identity check is not wired up (lode-pcee)"
    )


_GUARD = "scripts/assert-main-checkout.sh"
_MERGE_ONE = "scripts/land-merge-one.sh"


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
# Guard-coverage SWEEP (lode-1d2y, generalized in lode-8p3c) -- catches a NEW
# unguarded fence, not just the four fences four hand-anchored pins used to
# pin individually before lode-8p3c replaced them with this sweep.
#
# This module used to pin each already-discovered guarded fence individually,
# by anchoring on the commands it contained (a `_the_one_block` /
# `_the_two_reset_blocks` / `_assert_guard_precedes` trio, one pin per fence).
# That was precise, but closed-world: a genuinely NEW class of unguarded
# mutation -- `git clean -fdx`, `git rebase`, a `git checkout -f` with no
# accompanying reset, a bare `git commit -m` missing `--no-verify` -- matched
# none of those hand-picked selectors and was caught by NOTHING. That is
# exactly how lode-pxyt's own two exposures survived a full ticket cycle
# before a human noticed them by inspection. The sweep below is the
# open-world replacement -- lode-8p3c deleted the four per-fence pins outright
# once this sweep subsumed them, so there is now ONE answer to "is this fence
# covered", not five; `_unguarded_mutations` owns its mechanics.
#
# KNOWN LIMITATIONS (lode-1d2y):
#
# 1. INLINE COMMANDS ONLY -- SCRIPT-REFERENCE FOLLOWING IS DELIBERATELY OUT
#    OF SCOPE. This sweep does not open `scripts/*.sh` and classify what it
#    mutates; it only sees commands written directly in the fence. It would
#    NOT have caught lode-pxyt's first exposure on its own merits: Section
#    3's first-pass merge loop contains zero bare mutating git commands --
#    its only mutation is inside `scripts/land-merge-one.sh`, special-cased
#    below (`_MERGE_ONE`). A brand-new script reference is caught by nothing
#    here and would need the same manual discovery lode-pxyt's did. Measured
#    while writing this: of the scripts referenced
#    from an UNGUARDED fence today, none runs a cwd-resolved mutating git
#    command -- their only git calls are `merge-base --is-ancestor` and
#    `merge-tree --write-tree`, both read-only -- so the gap is latent, not
#    live. Re-measure rather than assume.
# 2. THE MUTATING-VERB REGEX IS DELIBERATELY OVER-BROAD, BY DESIGN. It
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

_MUTATING_VERB_RE = re.compile(
    r"\bgit\s+(?:add|am|apply|branch|checkout|cherry-pick|clean|commit|merge"
    r"|mv|pull|push|rebase|reset|restore|revert|rm|stash|switch|worktree)\b"
)

# Exact command text (comment-stripped, `.strip()`'d) -> why it needs no
# `scripts/assert-main-checkout.sh` guard. Keying on exact text is what makes
# an exemption reviewable data rather than prose; KNOWN LIMITATION 3 above
# records what that keying does and does not catch.
_KNOWN_LAND_SKILL_MUTATIONS: dict[str, str] = {
    # --- read-only false positives of the broad verb regex (limitation 2) ---
    'for mb in $(rtk git merge-base --all "origin/land/<X>" "origin/land/<Y>"); do': (
        "read-only: enumerates merge-bases, mutates nothing (1a stacked-branch "
        "detection)"
    ),
    'rtk git merge-base --is-ancestor "$mb" origin/trunk || OFF_TRUNK="$OFF_TRUNK $mb"': (
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
    "rtk git push origin trunk": (
        "ref-addressed (explicit remote+branch); Section 4's own text: cwd-independent"
    ),
    'rtk git push origin --delete "land/$id"': (
        "ref-addressed delete (explicit remote+branch); cwd-independent "
        "(Section 4's per-ticket branch GC)"
    ),
    'rtk git push origin --delete "land/<id>"': (
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
    "rtk git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true": (
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
            if not (_MUTATING_VERB_RE.search(cmd) or _MERGE_ONE in cmd):
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
    had to fix once in the per-fence pin next door.
    """
    for command in (
        "rtk git add .",
        "rtk git am /tmp/p.patch",
        "rtk git apply /tmp/p.patch",
        "rtk git checkout -f trunk",
        "rtk git cherry-pick deadbeef",
        "rtk git clean -fdx",
        "rtk git commit -m wip",
        "rtk git merge --no-ff origin/land/x",
        "rtk git pull --rebase",
        "rtk git rebase origin/trunk",
        "rtk git reset --hard HEAD~1",
        "rtk git restore --worktree .",
        "rtk git revert HEAD",
        "rtk git stash pop",
        "rtk git switch trunk",
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
    markdown = (
        f"```bash\nrtk git reset --hard origin/trunk\nrtk {_GUARD} || exit 1\n```\n"
    )
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
    original = "rtk git push origin trunk"
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

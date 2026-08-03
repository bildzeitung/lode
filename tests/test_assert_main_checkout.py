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

All tests run the ACTUAL `scripts/assert-main-checkout.sh` against real git
repositories (including real `git worktree add` checkouts and a real
submodule) built in `tmp_path` -- no fake git, no mocked subprocess --
sabotage-provable per the lode-verb bar: reverting the script back to
comparing `--show-toplevel` against itself would turn the worktree-refusal
test below green for the wrong reason, but the submodule/layout test would
catch a script that assumes `--git-common-dir` always ends in `/.git`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _gitrepo import _git
from conftest import bash_fence_blocks

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


def _fenced_bash(markdown: str) -> str:
    """The ```bash fences only -- what an agent actually EXECUTES.

    Scanning the whole file would match the prose that *explains* the old
    defect (it necessarily quotes the broken `-C "$(git rev-parse
    --show-toplevel)"` idiom), so the pin has to separate what is executed
    from what is merely described.

    Once deliberately NOT the same shape as tests/test_land_lock.py's
    `_fenced_bash`, which matched the fence marker at column 0 and was
    therefore blind to indented fences. lode-ovgs has since fixed that copy and
    unified it -- along with tests/test_land_conflicts_state.py's and
    tests/test_skill_bash_state.py's -- onto the shared
    `tests/conftest.py::bash_fence_blocks`, so the shapes now agree. This
    module is the one remaining private copy; folding it in too is lode-p4qb's
    job, deliberately sequenced after land/lode-gczf (which changes 178 lines
    of this file) so the deletion cannot merge cleanly into edited call sites.
    """
    return "\n".join(_fenced_bash_blocks(markdown))


def _fenced_bash_blocks(markdown: str) -> list[str]:
    """The ```bash fences kept SEPARATE, one string per block.

    Block boundaries are load-bearing here in a way they are not for
    `_fenced_bash`: per land/SKILL.md's governing rule (lode-sfnb) each fence
    is executed as its own Bash invocation, so two commands are guaranteed to
    share a shell -- and therefore `||` short-circuiting -- only if they are
    in the same block. A pin that flattens the blocks first cannot tell
    "guarded" from "merely preceded somewhere in the document".

    The fence marker is matched on the STRIPPED line, never at column 0. Four
    of land/SKILL.md's fences are indented under a markdown bullet, so a
    `line.startswith("```")` scanner -- the shape `tests/test_land_lock.py`
    used until `lode-ovgs` fixed it -- sees 20 of this file's
    24 bash blocks. That is not cosmetic for THIS module: one of the four it
    misses is Section 3's isolation-replay block, which runs its own
    `git reset --hard origin/trunk`. Under the column-0 shape the anchor below
    found exactly one reset block and looked correct; it was simply blind to
    the second. Measured on this file: 20 blocks vs 24, and 1 reset block vs 2.
    """
    blocks: list[str] = []
    current: list[str] = []
    in_bash = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_bash:
                blocks.append("\n".join(current))
                current = []
            in_bash = not in_bash and stripped in {"```bash", "```sh"}
            continue
        if in_bash:
            current.append(line)
    if in_bash and current:  # unterminated final fence
        blocks.append("\n".join(current))
    return blocks


def test_fence_scanner_sees_indented_fences() -> None:
    """Regression pin on the scanner SHAPE (lode-ovgs). A fence nested under a
    markdown bullet is indented, so a column-0 `line.startswith("```")` scanner
    never enters it. Four of land/SKILL.md's 24 bash fences are indented, and
    one of those is Section 3's isolation-replay block -- which runs its own
    `git reset --hard origin/trunk`.

    Without this pin, reverting `_fenced_bash_blocks` to the column-0 shape
    silently restores a state where the anchor below finds ONE reset block
    instead of two, and therefore looks correct while being blind to half the
    question it is asking. That is the same false-assurance failure mode this
    whole ticket exists to delete, so it gets a gate rather than a comment."""
    blocks = _fenced_bash_blocks(
        "1. Step one:\n\n   ```bash\n   echo indented\n   ```\n"
    )

    assert len(blocks) == 1, (
        "an indented ```bash fence was not parsed as a block -- the scanner has "
        "regressed to matching at column 0 (lode-ovgs)"
    )
    assert "echo indented" in blocks[0]


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
_PASS_START_RESET = "git reset --hard origin/trunk"
_SECTION1_ONLY = "git checkout -f trunk"
_MERGE_ONE = "scripts/land-merge-one.sh"
_REFORMAT_COMMIT = "git commit --no-verify"


def _the_one_block(
    *, contains: tuple[str, ...], excludes: tuple[str, ...] = (), label: str
) -> str:
    """The single executed fence matching `contains` and not `excludes`.

    Every guarded block in land/SKILL.md is located this way, so the selection
    lives here once. Anchoring on the COMMANDS a block runs, rather than on
    section headings, keeps the pins working while the surrounding prose is
    rewritten -- which it is constantly, by concurrent tickets.

    The `len(...) == 1` assertion is the load-bearing half, not a tidiness
    check: it is what makes a SECOND, unguarded copy of an already-guarded
    exposure fail loudly here instead of being silently absorbed by whichever
    narrower pin still happens to match one of them.
    """
    blocks = _fenced_bash_blocks(LAND_SKILL.read_text(encoding="utf-8"))
    candidates = [
        b
        for b in blocks
        if all(s in b for s in contains) and not any(s in b for s in excludes)
    ]
    assert len(candidates) == 1, (
        f"expected exactly one executed fence containing {list(contains)}"
        + (f" and none of {list(excludes)}" if excludes else "")
        + f" ({label}), found {len(candidates)} -- land/SKILL.md's layout has "
        "drifted and this pin needs re-anchoring, not deleting"
    )
    return candidates[0]


def _the_two_reset_blocks() -> tuple[str, str]:
    """`(Section 1's pass-start block, Section 3's isolation-replay block)`.

    Both open with `git reset --hard origin/trunk`, so that command does NOT
    identify Section 1 on its own -- an earlier column-0 fence scanner could
    not see Section 3's indented block and made it look as though it did (see
    `_fenced_bash_blocks`). Only Section 1's block also runs `git checkout -f
    trunk`, so that pair is what tells the two apart.
    """
    return (
        _the_one_block(
            contains=(_PASS_START_RESET, _SECTION1_ONLY),
            label="Section 1's pass-start block",
        ),
        _the_one_block(
            contains=(_PASS_START_RESET,),
            excludes=(_SECTION1_ONLY,),
            label="Section 3's isolation-replay block",
        ),
    )


def _assert_guard_precedes(
    block: str, *, protects: tuple[str, ...], section: str, ticket: str
) -> None:
    """Assert `block` calls the guard, and that every command in `protects` is
    still IN `block` and after it.

    Both halves are load-bearing, and the presence half is the easier one to
    lose. Per `land/SKILL.md`'s governing rule (lode-sfnb) each fenced block is
    a SEPARATE Bash invocation with no shell state carried between them, so a
    guard sitting in its own block can only `exit` that block's shell -- whether
    the destructive block then runs is an agent's judgment call made while
    reading prose, which is exactly the strength of assurance lode-pcee exists
    to delete. Sharing one block makes `|| exit 1` mechanical instead: each
    protected command is unreachable unless the assertion passed, with no
    decision in between.

    So an ordering-only pin is not enough. Refactoring a protected command out
    into a fence of its own re-opens the defect while leaving the ordering
    assertion perfectly green, which is why presence is checked too. Verified by
    mutation on land/SKILL.md: deleting the guard, reordering it after the reset,
    splitting it into its own fence, and splitting the replay loop (or the
    `land-merge-one.sh` call) out of Section 3's block are each caught by one of
    these two assertions -- and the last two by the presence half alone.
    """
    guard_at = block.find(_GUARD)
    assert guard_at >= 0, (
        f"{section}'s fenced block does not call {_GUARD} at all ({ticket}). A "
        "guard in a SEPARATE block cannot stop this one -- per lode-sfnb each "
        "block is its own Bash invocation, so its `exit` ends only "
        f"itself.\n\n{block}"
    )
    for mutation in protects:
        at = block.find(mutation)
        assert at >= 0, (
            f"`{mutation}` left {section}'s block -- if it moved to a fence of "
            "its own it is now reached with nothing having established where it "
            f"runs, which is the lode-pcee defect returning by another route "
            f"({ticket})\n\n{block}"
        )
        assert guard_at < at, (
            f"{_GUARD} runs AFTER `{mutation}` in {section}'s block ({ticket}) "
            f"-- the assertion no longer protects it.\n\n{block}"
        )


def test_guard_shares_one_block_with_the_commands_it_protects() -> None:
    """Section 1's pass-start block: the guard must be the first line of the
    SAME fence as every mutation it protects -- not merely present, and not
    merely earlier in the document. This is the property lode-pcee turns on;
    `_assert_guard_precedes` carries the reasoning and the mutation evidence.
    """
    section1, _ = _the_two_reset_blocks()

    # `bd dolt pull` writes the local Dolt DB; the rest write git. Only
    # `reset --hard` is unrecoverable, but a wrong-directory `checkout -f` is
    # destructive too.
    _assert_guard_precedes(
        section1,
        protects=(
            "bd dolt pull",
            _SECTION1_ONLY,
            "git fetch origin",
            _PASS_START_RESET,
        ),
        section="Section 1",
        ticket="lode-pcee",
    )


def test_section3_isolation_replay_block_shares_guard_with_its_reset() -> None:
    """Section 3's isolation-replay block (the 'Red' path's per-branch replay
    loop) runs its OWN `git reset --hard origin/trunk` -- a second, distinct
    exposure of the exact class lode-pcee fixed in Section 1, filed separately
    as lode-gczf since it could not be built until
    `scripts/assert-main-checkout.sh` existed on trunk.
    """
    _, section3 = _the_two_reset_blocks()

    # The pass-start reset is NOT this block's only cwd-assuming destructive
    # command, and `scripts/land-merge-one.sh` belongs on the list even though
    # it is a script rather than an inline git command: it runs a bare `git
    # merge --no-ff` plus `git restore --staged --worktree .beads/issues.jsonl`
    # and `git merge --abort`, all resolved against cwd.
    _assert_guard_precedes(
        section3,
        protects=(
            _PASS_START_RESET,
            _MERGE_ONE,
            "git reset --hard HEAD~1",
        ),
        section="Section 3",
        ticket="lode-gczf",
    )


def test_section3_first_pass_merge_loop_shares_guard_with_land_merge_one() -> None:
    """Section 3's FIRST-PASS ('Green') merge loop runs `land-merge-one.sh` --
    a bare `git merge --no-ff` against cwd -- in its own fresh Bash invocation
    that Section 1's guard cannot reach (lode-pxyt).

    The isolation-replay ('Red') loop lode-gczf already guarded calls the same
    script, and only IT also runs `_PASS_START_RESET`; excluding that is what
    tells the two apart.
    """
    _assert_guard_precedes(
        _the_one_block(
            contains=(_MERGE_ONE,),
            excludes=(_PASS_START_RESET,),
            label="Section 3's first-pass merge loop",
        ),
        protects=(_MERGE_ONE,),
        section="Section 3's first-pass merge loop",
        ticket="lode-pxyt",
    )


def test_section4_reformat_commit_block_shares_guard_with_its_commit() -> None:
    """Section 4's reformat-commit block commits `nox -t fix`'s output directly
    to whatever branch cwd's `HEAD` happens to be on -- the one `git` write in
    that section naming no ref or path at all, so a wrong-directory run commits
    there silently instead of failing loudly (lode-pxyt).

    `git add` is protected as well as the commit: it is the block's other
    cwd-resolved write, and without it here, splitting it out into a fence of
    its own leaves this whole module green (measured).
    """
    _assert_guard_precedes(
        _the_one_block(
            contains=(_REFORMAT_COMMIT,),
            label="Section 4's reformat-commit block",
        ),
        protects=("git add", _REFORMAT_COMMIT),
        section="Section 4's reformat-commit block",
        ticket="lode-pxyt",
    )


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
# Guard-coverage SWEEP (lode-1d2y) -- catches a NEW unguarded fence, not just
# the four already-known ones pinned above.
#
# Everything above this point pins a SPECIFIC, already-discovered fence by
# anchoring on the commands it contains (`_the_one_block` / `_the_two_reset_
# blocks`). That is precise, but closed-world: a DUPLICATE of an already-known
# exposure fails loudly (a third reset fence, a second bare-commit fence --
# `len(candidates) == 1` sees to that), but a genuinely NEW class of unguarded
# mutation -- `git clean -fdx`, `git rebase`, a `git checkout -f` with no
# accompanying reset, a bare `git commit -m` missing `--no-verify` -- matches
# none of the four `contains=` selectors above and is caught by NOTHING. That
# is exactly how lode-pxyt's own two exposures survived a full ticket cycle
# before a human noticed them by inspection.
#
# The sweep below is the open-world complement: it iterates EVERY fenced
# ```bash block in land/SKILL.md, flags every line matching a broad
# mutating-verb regex, and requires the guard to precede it UNLESS the exact
# (comment-stripped, whitespace-trimmed) command text is in `_KNOWN_LAND_
# SKILL_MUTATIONS` below, carrying its reason. A brand-new unguarded mutation
# fails here even though it fails NOWHERE above.
#
# KNOWN LIMITATIONS (recorded here, per this ticket's own acceptance
# criteria, not only in the bd ticket text -- lode-1d2y):
#
# 1. INLINE COMMANDS ONLY -- SCRIPT-REFERENCE FOLLOWING IS DELIBERATELY OUT
#    OF SCOPE. This sweep does not open `scripts/*.sh` and classify what it
#    mutates; it only sees commands written directly in the fence. It would
#    NOT have caught lode-pxyt's first exposure on its own merits: Section
#    3's first-pass merge loop contains zero bare mutating git commands --
#    its only mutation is inside `scripts/land-merge-one.sh`. That one named
#    script is special-cased below (`_MERGE_ONE`, exactly as the per-fence
#    pins above already treat it) precisely because a previous ticket already
#    established it needs the guard; a brand-new script reference introduced
#    by some future edit is NOT caught by this sweep and would need the same
#    kind of manual discovery lode-pxyt's did. Building a general classifier
#    that opens arbitrary referenced scripts and decides whether THEY mutate
#    cwd is out of scope for this ticket -- it is a materially bigger, open-
#    ended undertaking (shell parsing of arbitrary script bodies), not a
#    small extension of this sweep. This is an explicit, deliberate decision,
#    not an oversight: state it plainly rather than claim airtight coverage.
# 2. COMMENT-STRIPPING IS A LINE-LEVEL HEURISTIC, NOT A SHELL TOKENIZER.
#    `_strip_bash_comments` below removes everything from the first `#` that
#    is preceded by start-of-line or whitespace, per line. It cannot tell a
#    real comment from a `#` embedded inside a quoted string preceded by
#    whitespace (e.g. a commit message body containing `"fix #123"`) -- such
#    a line would be silently truncated mid-command. Verified against
#    land/SKILL.md's actual content as of this ticket: no line in any fence
#    has a `#` inside an open quote (checked by counting quote characters
#    before every ` #`/`^#` match), so this is latent, not live, exactly like
#    several other "measured on today's corpus, re-measure if it changes"
#    caveats already in this codebase (`tests/conftest.py::bash_fence_blocks`'s
#    own docstring carries the same style of caveat).
# 3. THE MUTATING-VERB REGEX IS DELIBERATELY OVER-BROAD, BY DESIGN. It
#    matches `git branch`, `git worktree`, and `git merge` as whole verbs,
#    not just the destructive subcommands (`branch -D`, `worktree remove`,
#    a bare `merge`) -- so it also flags read-only lookalikes: `git merge-
#    base` (a plain substring/prefix match on `merge` finds it), `git branch
#    --merged`, `git worktree list`. This is the exact class of false
#    positive this ticket's own text calls out. Rather than hand-craft a
#    narrower regex that tries to exclude every read-only shape (and inherit
#    the risk of excluding a genuinely dangerous one by the same narrowing),
#    every such false positive is handled the same way as a genuine, reasoned
#    exemption: a per-command allowlist entry recording WHY it needs no
#    guard. That keeps the regex simple and auditable, and pushes the
#    judgment calls into visible, reviewable data instead of regex cleverness.
# ---------------------------------------------------------------------------

_MUTATING_VERB_RE = re.compile(
    r"\bgit\s+(?:reset|clean|rebase|checkout|commit|merge|push|rm|mv|restore"
    r"|branch|worktree)\b"
)

# Comment stripping: remove everything from a `#` that starts at the
# beginning of the line or is preceded by whitespace, through end of line.
# See KNOWN LIMITATION 2 above.
_BASH_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")


def _strip_bash_comments(block: str) -> str:
    return "\n".join(_BASH_COMMENT_RE.sub("", line) for line in block.splitlines())


# Exact command string (post comment-strip, `.strip()`'d) -> why it needs no
# `scripts/assert-main-checkout.sh` guard. Per this ticket's acceptance
# criteria, exemption is DATA, keyed on exact text -- if the corresponding
# line in land/SKILL.md changes shape even cosmetically (a renamed variable,
# an added flag), the key no longer matches and the sweep below re-flags the
# new text as an unguarded candidate, which must then either gain a guard or
# a fresh, deliberate allowlist entry. Nothing here is "trust this forever."
_KNOWN_LAND_SKILL_MUTATIONS: dict[str, str] = {
    # --- read-only false positives of the broad verb regex (limitation 3) ---
    'for mb in $(rtk git merge-base --all "origin/land/<X>" "origin/land/<Y>"); do': "read-only: enumerates merge-bases, mutates nothing (1a stacked-branch "
    "detection)",
    'rtk git merge-base --is-ancestor "$mb" origin/trunk || OFF_TRUNK="$OFF_TRUNK $mb"': "read-only: --is-ancestor is a pure query, mutates nothing",
    'LOCK_REASON=$(git worktree list --porcelain | awk -v want="$WT" \'': "read-only listing (Section 4 worktree-GC loop's stale-lock reason lookup)",
    "done < <(git worktree list --porcelain | awk '": "read-only listing (Section 4 worktree-GC loop's candidate enumeration)",
    "MERGED=$(git branch --merged trunk --format='%(refname:short)')": "read-only listing (Section 4's third backstop, computing the merged set)",
    "CHECKED_OUT=$(git worktree list --porcelain | awk "
    "'/^branch refs\\/heads\\//{print substr($0,19)}')": "read-only listing (Section 4's third backstop, computing checked-out branches)",
    # --- explicit ref/path-addressed writes: cwd-independent by construction,
    #     the same reasoning Section 4's own prose gives for these commands
    #     ("each names its own target") ---
    "rtk git push origin trunk": "ref-addressed (explicit remote+branch); Section 4's own text: cwd-independent",
    'rtk git push origin --delete "land/$id"': "ref-addressed delete (explicit remote+branch); cwd-independent "
    "(Section 4's per-ticket branch GC)",
    'rtk git push origin --delete "land/<id>"': "ref-addressed delete (explicit remote+branch); cwd-independent "
    "(Bounce / Escalate-exit-(b) / Escalate-exit-(c))",
    'git worktree unlock "$WT" 2>/dev/null || true': "path-addressed to $WT (the exact worktree path just read from "
    "`git worktree list --porcelain`), not cwd-resolved",
    'if git worktree remove --force "$WT"; then': "path-addressed to $WT, not cwd-resolved (same reasoning as worktree "
    "unlock above)",
    '[ -n "$BR" ] && git branch -D "$BR" 2>/dev/null || true': "ref-addressed to $BR, an explicit branch name read from the same "
    "porcelain listing, not cwd-resolved",
    'if git branch -D "$BR" 2>/dev/null; then': "ref-addressed to $BR, an explicit branch name, not cwd-resolved",
    "git worktree prune": "housekeeping only: drops stale worktree admin entries already gone "
    "from disk; never removes real content or a live worktree",
    # --- path-addressed to the passive bd export, never real work ---
    "rtk git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true": "path-addressed to the passive .beads/issues.jsonl export only -- "
    "never real work (import.auto: false, lode-6ra); a wrong-directory "
    "run only restores that worktree's own copy of a regenerated artifact",
}


def _unguarded_mutations(markdown: str, *, allowlist: dict[str, str]) -> list[str]:
    """Every fenced ```bash block's mutating command that is neither
    allowlisted nor preceded, in its OWN block, by `scripts/assert-main-
    checkout.sh`. Empty means full coverage. See the module-level comment
    above for the regex/allowlist design and its known limitations.
    """
    violations: list[str] = []
    for block_index, block in enumerate(bash_fence_blocks(markdown)):
        stripped = _strip_bash_comments(block)
        guard_pos = stripped.find(_GUARD)
        for line in stripped.splitlines():
            cmd = line.strip()
            if not cmd:
                continue
            is_mutation = bool(_MUTATING_VERB_RE.search(cmd)) or (_MERGE_ONE in cmd)
            if not is_mutation or cmd in allowlist:
                continue
            line_pos = stripped.find(line)
            if guard_pos == -1 or guard_pos >= line_pos:
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
    guarded or a recorded, reasoned exemption -- not just the four fences the
    pins above happen to anchor on.
    """
    violations = _unguarded_mutations(
        LAND_SKILL.read_text(encoding="utf-8"),
        allowlist=_KNOWN_LAND_SKILL_MUTATIONS,
    )
    assert violations == [], "\n".join(violations)


def test_sweep_catches_a_brand_new_unguarded_mutation() -> None:
    """The property this whole ticket exists to add: a mutating fence with NO
    guard at all, and not present in any allowlist, must be caught -- this is
    the lode-pxyt-class exposure (a genuinely new unguarded block) that none
    of the pins above would have caught on their own."""
    markdown = "1. Some new step:\n\n   ```bash\n   rtk git clean -fdx\n   ```\n"
    violations = _unguarded_mutations(markdown, allowlist={})
    assert violations, "an unguarded, unallowlisted `git clean -fdx` was not flagged"
    assert "git clean -fdx" in violations[0]


def test_sweep_requires_the_guard_precede_not_merely_be_present() -> None:
    """A guard call present in the block but AFTER the mutating command does
    not protect it -- ordering matters, exactly as `_assert_guard_precedes`
    already checks for the four pinned fences above."""
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
    candidate needing its own guard or its own fresh allowlist entry."""
    original = "rtk git push origin trunk"
    assert original in _KNOWN_LAND_SKILL_MUTATIONS, (
        "fixture assumption broken -- pick a different real allowlist entry"
    )
    perturbed = original + " --porcelain"
    markdown = f"```bash\n{perturbed}\n```\n"

    violations = _unguarded_mutations(markdown, allowlist=_KNOWN_LAND_SKILL_MUTATIONS)
    assert violations, (
        "a cosmetically-changed allowlisted command must not silently keep "
        "matching its old allowlist entry"
    )

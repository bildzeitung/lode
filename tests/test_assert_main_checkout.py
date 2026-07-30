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

import subprocess
from pathlib import Path

from _gitrepo import _git

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


def test_guard_shares_one_block_with_the_commands_it_protects() -> None:
    """The guard must be the first line of the SAME fenced block as every
    mutation it protects -- not merely present, and not merely earlier in the
    document.

    This is the property the whole ticket turns on. Per `land/SKILL.md`'s own
    governing rule (lode-sfnb), each fenced block is executed as a SEPARATE
    Bash invocation with no shell state carried between them, so a guard
    sitting in its own block can only `exit` that block's shell -- whether the
    destructive block runs next is then an agent's judgment call made while
    reading prose. Sharing one block is what makes `|| exit 1` mechanical:
    `git reset --hard origin/trunk` becomes unreachable unless the assertion
    passed, with no decision in between. Splitting the fences back apart, or
    reordering within the block, restores exactly the defect lode-pcee fixed
    -- an unrecoverable hard reset reached with nothing having established
    where it is running. Both regressions are verified by mutation, and both
    leave every other pin in this module green.
    """
    blocks = _fenced_bash_blocks(LAND_SKILL.read_text(encoding="utf-8"))

    # Anchor on the pass-start hard reset -- the unrecoverable command -- but it
    # is NOT unique to Section 1: Section 3's isolation-replay block opens with
    # its own `git reset --hard origin/trunk` before re-deriving $STATE_DIR.
    # (An earlier column-0 fence scanner could not see that indented block and
    # made this anchor look unique; see `_fenced_bash_blocks`.) Section 1 is
    # identified by the PAIR -- only its block also runs `git checkout -f
    # trunk`. Anchoring on commands rather than a section heading keeps the pin
    # working when the surrounding prose is rewritten, which it is constantly,
    # by concurrent tickets.
    #
    # Section 3's reset is genuinely unguarded and is the same exposure class
    # one section over, but it is outside this ticket's stated scope (Section 1)
    # and cannot be built until scripts/assert-main-checkout.sh exists on trunk.
    # Tracked by lode-gczf, which blocks on lode-pcee.
    owning = [
        b
        for b in blocks
        if "git reset --hard origin/trunk" in b and "git checkout -f trunk" in b
    ]
    assert len(owning) == 1, (
        "expected exactly one executed fence to run BOTH `git reset --hard "
        f"origin/trunk` and `git checkout -f trunk`, found {len(owning)} -- "
        "land/SKILL.md's layout has drifted and this pin needs re-anchoring, "
        "not deleting"
    )
    block = owning[0]

    guard_at = block.find("scripts/assert-main-checkout.sh")
    assert guard_at >= 0, (
        "the fenced block that runs `git reset --hard origin/trunk` does not call "
        "scripts/assert-main-checkout.sh at all (lode-pcee). A guard in a SEPARATE "
        "block cannot stop this one -- per lode-sfnb each block is its own Bash "
        f"invocation, so its `exit` ends only itself.\n\n{block}"
    )

    # ...and before every state-changing command sharing that block. `bd dolt
    # pull` writes the local Dolt DB; the rest write git. Only `reset --hard` is
    # unrecoverable, but a wrong-directory `checkout -f` is destructive too.
    for mutation in (
        "bd dolt pull",
        "git checkout -f trunk",
        "git fetch origin",
        "git reset --hard origin/trunk",
    ):
        at = block.find(mutation)
        assert at >= 0, (
            f"{mutation!r} left Section 1's block -- if it moved somewhere "
            "unguarded, that is the lode-pcee defect returning by another route"
        )
        assert guard_at < at, (
            f"scripts/assert-main-checkout.sh runs AFTER `{mutation}` in the same "
            f"block (lode-pcee) -- the assertion no longer protects it.\n\n{block}"
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

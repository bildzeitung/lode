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

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "assert-main-checkout.sh"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


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
    from what is merely described. Same helper shape as
    tests/test_land_lock.py's `_fenced_bash`.
    """
    blocks: list[str] = []
    in_bash = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_bash = not in_bash and line.strip() in {"```bash", "```sh"}
            continue
        if in_bash:
            blocks.append(line)
    return "\n".join(blocks)


def test_land_skill_section1_calls_the_script() -> None:
    """Section 1 must actually invoke the guard, not just describe it in
    prose -- the defect this ticket fixes lived in a markdown fence, where no
    other gate reaches it."""
    executed = _fenced_bash(LAND_SKILL.read_text(encoding="utf-8"))
    assert "scripts/assert-main-checkout.sh" in executed, (
        "land/SKILL.md Section 1 never calls scripts/assert-main-checkout.sh -- "
        "the main-checkout identity check is not wired up (lode-pcee)"
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

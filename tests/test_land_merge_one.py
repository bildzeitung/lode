"""Tests for scripts/land-merge-one.sh (lode-sfnb).

`/land`'s Section 3 merge loop used to define an inline bash FUNCTION
(`merge_one()`) that read a bash ASSOCIATIVE ARRAY (`MSG`) populated by a
*separate*, earlier fenced code block in `.claude/skills/land/SKILL.md`
(Section 3a's precompute step). An agent executing the skill runs each fenced
block as its own Bash tool invocation, and shell state -- variables, arrays,
function definitions -- does not persist between invocations. By the time the
merge loop ran, `MSG` was empty and `merge_one` may not even have been
(re)declared, so `${MSG[$id]}` silently expanded to the empty string and
`git merge -m ''` either produced an empty-message merge or the surrounding
reconstruction failed with no output at all. OBSERVED landing the 2026-07-26
lode-ns3r/lode-1q2i/lode-sys4 pass (see the ticket body for the exact
reproduction: `declare -A MSG` re-declared over a variable a prior `source`
had already created as an INDEXED array, so bash refused to convert and
exited non-zero with completely empty stdout/stderr).

This script extracts the merge step to a file on disk: a script is available
identically to every Bash invocation that calls it, with no in-memory bash
state that needs to survive between one fenced block and the next. The
commit message itself is passed as a file path (`<land-msg-dir>/<id>`,
written once by /land's Section 3a precompute step) rather than a bash
associative array, for the same reason -- a file on disk survives a
`git reset --hard` and a fresh Bash invocation; a bash variable does not.

All tests below run the ACTUAL `scripts/land-merge-one.sh` against real git
repositories built in `tmp_path` -- no fake git, no mocked subprocess,
matching the sabotage-provable bar the other extracted-script test suites in
this repo use (see tests/test_merge_precheck.py's own header).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-merge-one.sh"


def _assert_machine_fault_contract(stderr: str) -> None:
    """Every exit-2 path must emit the WHOLE shared machine-fault contract.

    `scripts/merge-precheck.sh`, `scripts/validate-mermaid.sh` and
    `scripts/release-bump.sh` all open an exit-2 diagnostic with the same
    ``GATE COULD NOT RUN:`` banner and close it with the same standing
    instruction not to blame a branch for it (lode-9i2p). Emitting only half
    of that is exactly how a machine fault gets read as a branch verdict, so
    the contract is asserted here rather than left to convention.

    Presence, not position: the unexpected-git-failure path deliberately
    echoes git's own error out first, so the banner is not always the first
    byte of stderr.
    """
    assert "GATE COULD NOT RUN:" in stderr, stderr
    assert "machine fault a human must fix" in stderr, stderr
    assert "do not kick this branch back needs-rebase" in stderr, stderr


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit on `trunk`, isolated user config.

    `origin/land/<id>` is faked as a plain local branch of that name -- the
    script only ever does `git merge --no-ff origin/land/$id`, which needs a
    resolvable ref, not an actual configured remote.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("line1\nline2\nline3\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _add_worktree(repo: Path, rel_path: str, branch: str) -> Path:
    """A real linked worktree of `repo` -- not the main checkout."""
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, "trunk")
    return wt


def _branch_from(repo: Path, base: str, name: str) -> None:
    _git(repo, "checkout", "-q", base)
    _git(repo, "checkout", "-q", "-b", name)


def _commit_file(repo: Path, path: str, content: str, message: str) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)


def _write_msg(msg_dir: Path, id_: str, message: str) -> None:
    msg_dir.mkdir(parents=True, exist_ok=True)
    (msg_dir / id_).write_text(message)


def _run(
    id_: str, msg_dir: Path, repo: Path, *, on_branch: str = "trunk"
) -> subprocess.CompletedProcess:
    _git(repo, "checkout", "-q", on_branch)
    return subprocess.run(
        ["bash", str(SCRIPT), id_, str(msg_dir)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_clean_merge_exits_0_and_uses_the_precomputed_message(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: does a thing (lode-a)")

    result = _run("lode-a", msg_dir, repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    log = _git(repo, "log", "-1", "--pretty=%B", "trunk")
    assert log.stdout.rstrip("\n") == "Merge land/lode-a: does a thing (lode-a)"
    assert (repo / "a.txt").read_text() == "from A\n"
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""


def test_clean_merge_heartbeats_the_single_lander_lock(tmp_path: Path) -> None:
    """lode-m87j: every call to this script must re-stamp the single-lander
    lock (scripts/land-lock.sh heartbeat), because this script is the sole
    call site that covers BOTH of /land Section 3's merge loops (the first
    pass and the isolation-replay copy) -- a dropped heartbeat here silently
    reintroduces the acquisition-age exposure the ticket exists to close.
    Behavioural, not a source grep: seeds a lock file that LOOKS old, then
    asserts its recorded epoch actually advanced after the script ran."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-hb")
    _commit_file(repo, "hb.txt", "from HB\n", "HB adds hb.txt")
    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-hb", "Merge land/lode-hb: heartbeat check (lode-hb)")

    lock = repo / ".git" / "land.lock"
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run("lode-hb", msg_dir, repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert lock.exists(), "the heartbeat call must not delete the lock file"
    new_epoch = int(lock.read_text().split()[2])
    assert new_epoch > old_epoch, (
        "scripts/land-merge-one.sh did not heartbeat scripts/land-lock.sh -- "
        "the lock's recorded epoch was not refreshed (lode-m87j)"
    )


def test_missing_message_file_exits_2_loud_never_empty_message_merge(
    tmp_path: Path,
) -> None:
    """The exact failure this ticket exists to close: a missing/empty
    precomputed message must never silently produce an empty-message merge.
    It must refuse loudly instead -- exit 2, a clear stderr diagnostic, and
    NO merge commit created at all."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-b")
    _commit_file(repo, "b.txt", "from B\n", "B adds b.txt")
    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()  # empty -- no file for lode-b

    before = _git(repo, "rev-parse", "trunk").stdout.strip()
    result = _run("lode-b", msg_dir, repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "no precomputed merge message" in result.stderr
    assert "lode-b" in result.stderr
    _assert_machine_fault_contract(result.stderr)
    after = _git(repo, "rev-parse", "trunk").stdout.strip()
    assert before == after, "no merge should have happened"


def test_empty_message_file_is_also_refused(tmp_path: Path) -> None:
    """A message FILE that exists but is empty (e.g. a `bd show` that
    returned nothing) must be treated the same as missing -- `[ -s ... ]`
    tests non-empty, not merely existence."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-c")
    _commit_file(repo, "c.txt", "from C\n", "C adds c.txt")
    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-c", "")

    result = _run("lode-c", msg_dir, repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "no precomputed merge message" in result.stderr


def test_real_conflict_exits_1_prints_paths_and_leaves_a_clean_tree(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "already-merged")
    _commit_file(repo, "f.txt", "CHANGED-ON-TRUNK\nline2\nline3\n", "trunk changes f")
    _git(repo, "checkout", "-q", "trunk")
    _git(repo, "merge", "-q", "--no-ff", "already-merged", "-m", "fold in trunk change")

    _branch_from(repo, "trunk", "origin/land/lode-d")
    _git(repo, "checkout", "-q", "origin/land/lode-d")
    _git(repo, "reset", "-q", "--hard", "trunk~1")  # branch off BEFORE trunk's change
    _commit_file(
        repo, "f.txt", "CHANGED-BY-BRANCH\nline2\nline3\n", "branch changes f too"
    )

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-d", "Merge land/lode-d: conflicting change (lode-d)")

    result = _run("lode-d", msg_dir, repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == "f.txt\n"
    # The merge must be fully aborted -- no MERGE_HEAD, no dirty tree, no
    # unmerged index entries left lying around for the next command.
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""
    unmerged = _git(repo, "ls-files", "-u")
    assert unmerged.stdout == ""


def test_staged_jsonl_trap_is_retried_and_succeeds(tmp_path: Path) -> None:
    """The passive `.beads/issues.jsonl` export (import.auto: false, lode-6ra
    -- never real work) can end up STAGED with content that differs from
    what the merge needs to write, which makes git refuse with 'would be
    overwritten by merge' even though `git ls-files -u` is empty (no actual
    conflict). The script must recognize this, restore the export, retry
    ONCE, and succeed -- never surface it as a conflict or a machine fault."""
    repo = _init_repo(tmp_path)
    _commit_file(repo, ".beads/issues.jsonl", "A\n", "seed jsonl on trunk")

    _branch_from(repo, "trunk", "origin/land/lode-e")
    _commit_file(repo, ".beads/issues.jsonl", "B\n", "branch updates jsonl")
    _commit_file(repo, "e.txt", "from E\n", "branch adds e.txt")

    _git(repo, "checkout", "-q", "trunk")
    (repo / ".beads" / "issues.jsonl").write_text("C\n")
    _git(repo, "add", ".beads/issues.jsonl")  # staged, uncommitted -- the trap

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-e", "Merge land/lode-e: jsonl trap retry (lode-e)")

    result = subprocess.run(
        ["bash", str(SCRIPT), "lode-e", str(msg_dir)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "e.txt").read_text() == "from E\n"
    assert (repo / ".beads" / "issues.jsonl").read_text() == "B\n"
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""


def test_staged_interactions_jsonl_trap_is_retried_and_succeeds(tmp_path: Path) -> None:
    """The SAME trap, sprung by the OTHER passive export (lode-2nw5). The retry used to
    restore `.beads/issues.jsonl` alone; it now restores every entry on
    `scripts/beads-passive-exports.txt`, so `.beads/interactions.jsonl` -- which the
    pre-commit hook regenerates and restages in the very same commit -- is covered too.

    This is the behavioural half of the pin: `tests/test_land_merge_one_passive_exports.py`
    asserts over the script's TEXT, which cannot tell whether the restore actually reaches
    this path. Note the sibling test above deliberately seeds ONLY `issues.jsonl`, leaving
    `interactions.jsonl` unknown to git in that repo -- between the two, a restore that is
    atomic over its pathspecs (and so restores NOTHING when one is unknown) is red either
    way, which is exactly the regression this pair exists to catch."""
    repo = _init_repo(tmp_path)
    _commit_file(repo, ".beads/interactions.jsonl", "A\n", "seed interactions on trunk")

    _branch_from(repo, "trunk", "origin/land/lode-i")
    _commit_file(
        repo, ".beads/interactions.jsonl", "B\n", "branch updates interactions"
    )
    _commit_file(repo, "i.txt", "from I\n", "branch adds i.txt")

    _git(repo, "checkout", "-q", "trunk")
    (repo / ".beads" / "interactions.jsonl").write_text("C\n")
    _git(repo, "add", ".beads/interactions.jsonl")  # staged, uncommitted -- the trap

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-i", "Merge land/lode-i: interactions trap retry (lode-i)")

    result = subprocess.run(
        ["bash", str(SCRIPT), "lode-i", str(msg_dir)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "i.txt").read_text() == "from I\n"
    assert (repo / ".beads" / "interactions.jsonl").read_text() == "B\n"
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""


def test_unexpected_git_failure_exits_2_not_1(tmp_path: Path) -> None:
    """A git failure that is neither the jsonl trap nor a real conflict (git
    ls-files -u stays empty) must be treated as a machine fault -- exit 2,
    never misread as exit 1 (a content conflict) or silently retried forever.
    Reproduced with an untracked working-tree file colliding with a tracked
    file the branch introduces: git's own message ('The following untracked
    working tree files would be overwritten by merge') contains the same
    substring the jsonl-trap check greps for, so the retry fires once,
    fails identically, and the script must still land on exit 2."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-f")
    _commit_file(repo, "untracked.txt", "from branch\n", "branch adds untracked.txt")

    _git(repo, "checkout", "-q", "trunk")
    (repo / "untracked.txt").write_text("local, never added to git\n")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-f", "Merge land/lode-f: untracked collision (lode-f)")

    result = _run("lode-f", msg_dir, repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == "", (
        "exit 2 must print nothing the caller could capture as $CONFLICTS"
    )
    _assert_machine_fault_contract(result.stderr)
    unmerged = _git(repo, "ls-files", "-u")
    assert unmerged.stdout == ""


def test_not_main_checkout_exits_2_before_attempting_any_merge(tmp_path: Path) -> None:
    """lode-1nty: run from a linked worktree (not the main checkout), the
    script must refuse before ever calling `git merge` -- exit 2 (machine
    fault, never the exit-1 conflict code), the shared
    `_assert_machine_fault_contract`, AND `scripts/assert-main-checkout.sh`'s
    own diagnostic naming the mismatch. A real precomputed message is
    provided so a bug that checked main-checkout identity AFTER the
    missing-message check would still be caught -- this asserts the identity
    check runs first, not merely that some exit-2 fires."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-g")
    _commit_file(repo, "g.txt", "from G\n", "G adds g.txt")
    wt = _add_worktree(repo, "wt", "some-other-branch")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-g", "Merge land/lode-g: from a worktree (lode-g)")

    before = _git(repo, "rev-parse", "trunk").stdout.strip()
    result = subprocess.run(
        ["bash", str(SCRIPT), "lode-g", str(msg_dir)],
        cwd=wt,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == "", (
        "exit 2 must print nothing the caller could capture as $CONFLICTS"
    )
    _assert_machine_fault_contract(result.stderr)
    assert "NOT RUNNING IN THE MAIN CHECKOUT" in result.stderr, result.stderr
    after = _git(repo, "rev-parse", "trunk").stdout.strip()
    assert before == after, "no merge should have been attempted at all"
    unmerged = _git(wt, "ls-files", "-u")
    assert unmerged.stdout == ""


@pytest.mark.parametrize("argv", [[], ["lode-a"], ["lode-a", "msgdir", "extra"]])
def test_wrong_arg_count_is_exit_2_never_1(tmp_path: Path, argv: list[str]) -> None:
    """A caller bug must never land in the CONFLICT code. Exit 1 is reserved
    for a real textual conflict, so a bad arg count exits 2 -- the same reason
    `scripts/merge-precheck.sh` checks `$#` before anything else rather than
    relying on `${1:?}` (whose exit 1 would collide).

    Run with an explicit throwaway main-checkout `cwd` (lode-1nty): this
    script now asserts main-checkout identity BEFORE the arg-count check, so
    without a real main checkout here this test would instead exercise (and
    only prove) that earlier guard -- not the arg-count check it's named for.
    """
    repo = _init_repo(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
    _assert_machine_fault_contract(result.stderr)

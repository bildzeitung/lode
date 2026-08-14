"""Tests for scripts/land-merge-batch.sh (lode-s9xe.4).

`/land`'s Section 3 has TWO merge loops with the identical shape -- the
first-pass batch merge and the isolation-replay copy run after a
`git reset --hard origin/trunk` -- fenced separately in
`.claude/skills/land/SKILL.md` with a comment asking a human to "keep the two
loops the same shape", an unenforced sync invariant over destructive code.
This script is the one copy both call sites are meant to drive.

All tests below run the ACTUAL `scripts/land-merge-batch.sh` against real git
repositories built in `tmp_path`, driving its real dependencies
(`land-merge-one.sh`, `drop-from-accepted.sh`, `land-state-load.sh`) rather
than mocking any of them -- the same sabotage-provable bar
tests/test_land_merge_one.py and tests/test_drop_from_accepted.py use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-merge-batch.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit on `trunk`.

    `origin/land/<id>` branches are faked as plain local branches of that
    name -- land-merge-one.sh only ever does `git merge --no-ff
    origin/land/$id`, which needs a resolvable ref, not a configured remote.
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


def _accepted(tmp_path: Path, *ids: str) -> Path:
    p = tmp_path / "accepted"
    p.write_text("".join(f"{i}\n" for i in ids))
    return p


def _graph(tmp_path: Path, *edges: tuple[str, str, str]) -> Path:
    p = tmp_path / "graph"
    p.write_text("".join(f"EDGE\t{d}\t{b}\t{k}\n" for d, b, k in edges))
    return p


def _run(
    repo: Path,
    accepted: Path,
    msg_dir: Path,
    conflicts_dir: Path,
    *,
    graph: Path | None = None,
    landed: Path | None = None,
    on_branch: str = "trunk",
) -> subprocess.CompletedProcess:
    _git(repo, "checkout", "-q", on_branch)
    args = [
        "bash",
        str(SCRIPT),
        "--accepted",
        str(accepted),
        "--msg-dir",
        str(msg_dir),
        "--conflicts-dir",
        str(conflicts_dir),
    ]
    if graph is not None:
        args += ["--graph", str(graph)]
    if landed is not None:
        args += ["--landed", str(landed)]
    return subprocess.run(
        args, cwd=repo, capture_output=True, text=True, timeout=30, check=False
    )


def test_two_clean_merges_land_and_the_accepted_file_is_unchanged(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    _branch_from(repo, "trunk", "origin/land/lode-b")
    _commit_file(repo, "b.txt", "from B\n", "B adds b.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    _write_msg(msg_dir, "lode-b", "Merge land/lode-b: B (lode-b)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a", "lode-b")
    landed = tmp_path / "landed"
    landed.touch()

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed=landed)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\nLANDED\tlode-b\n"
    assert accepted.read_text() == "lode-a\nlode-b\n", (
        "a clean land must not remove the id from --accepted"
    )
    assert landed.read_text() == "lode-a\nlode-b\n"
    assert (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()


def test_landed_file_is_optional(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")

    result = _run(repo, accepted, msg_dir, conflicts_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\n"


def test_a_real_conflict_drops_the_id_and_continues_with_the_rest(
    tmp_path: Path,
) -> None:
    """lode-b's branch is cut BEFORE lode-a merges and touches the same
    lines -- once lode-a lands, lode-b conflicts against the new trunk. The
    conflicting id must leave --accepted (rewritten in place) and the loop
    must continue on to the next id rather than aborting the whole batch."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "f.txt", "A-CHANGE\nline2\nline3\n", "A changes f")

    _branch_from(repo, "trunk", "origin/land/lode-b")
    _commit_file(repo, "f.txt", "B-CHANGE\nline2\nline3\n", "B changes f too")

    _branch_from(repo, "trunk", "origin/land/lode-c")
    _commit_file(repo, "c.txt", "from C\n", "C adds c.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (("lode-a", "A"), ("lode-b", "B"), ("lode-c", "C")):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a", "lode-b", "lode-c")

    result = _run(repo, accepted, msg_dir, conflicts_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\nCONFLICT\tlode-b\nLANDED\tlode-c\n"
    assert accepted.read_text() == "lode-a\nlode-c\n", (
        "the conflicting id must be rewritten out of --accepted"
    )
    assert (conflicts_dir / "lode-b").read_text() == "f.txt\n"
    assert (repo / "c.txt").exists()
    # the merge left a clean tree behind
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""
    unmerged = _git(repo, "ls-files", "-u")
    assert unmerged.stdout == ""


def test_a_conflicting_base_holds_its_dependent_too(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-x")
    _commit_file(repo, "f.txt", "X-CHANGE\nline2\nline3\n", "X changes f")

    _branch_from(repo, "trunk", "origin/land/lode-base")
    _commit_file(repo, "f.txt", "BASE-CHANGE\nline2\nline3\n", "base changes f too")

    _branch_from(repo, "trunk", "origin/land/lode-dep")
    _commit_file(repo, "dep.txt", "from dep\n", "dep adds dep.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (("lode-x", "X"), ("lode-base", "BASE"), ("lode-dep", "DEP")):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-x", "lode-base", "lode-dep")
    graph = _graph(tmp_path, ("lode-dep", "lode-base", "direct"))

    result = _run(repo, accepted, msg_dir, conflicts_dir, graph=graph)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines == ["LANDED\tlode-x", "CONFLICT\tlode-base", "HELD\tlode-dep"]
    assert accepted.read_text() == "lode-x\n"
    assert not (repo / "dep.txt").exists(), "a HELD id must never be merged"


def test_a_machine_fault_stops_processing_the_rest_of_the_batch(
    tmp_path: Path,
) -> None:
    """No precomputed message for the middle id -- land-merge-one.sh exits 2.
    Per lode-9i2p this must never be read as a conflict, and the id after it
    must not be touched at all: its fate is unknown, not silently decided."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-p")
    _commit_file(repo, "p.txt", "from P\n", "P adds p.txt")
    _branch_from(repo, "trunk", "origin/land/lode-q")
    _commit_file(repo, "q.txt", "from Q\n", "Q adds q.txt")
    _branch_from(repo, "trunk", "origin/land/lode-r")
    _commit_file(repo, "r.txt", "from R\n", "R adds r.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-p", "Merge land/lode-p: P (lode-p)")
    # no message written for lode-q
    _write_msg(msg_dir, "lode-r", "Merge land/lode-r: R (lode-r)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-p", "lode-q", "lode-r")
    landed = tmp_path / "landed"
    landed.touch()

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed=landed)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-p\n"
    assert landed.read_text() == "lode-p\n"
    assert not (repo / "r.txt").exists(), "an id after the fault must never be merged"
    assert "no precomputed merge message" in result.stderr


def test_missing_accepted_file_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()

    result = _run(repo, tmp_path / "nope", msg_dir, conflicts_dir)

    assert result.returncode == 2
    assert result.stdout == ""


def test_empty_accepted_file_iterates_zero_times(tmp_path: Path) -> None:
    """A present-but-empty accepted set is a legitimate all-bounced /
    all-kicked-back outcome (lode-0jan's rule), not a fault."""
    repo = _init_repo(tmp_path)
    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = tmp_path / "accepted"
    accepted.write_text("")

    result = _run(repo, accepted, msg_dir, conflicts_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_missing_graph_file_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")

    result = _run(repo, accepted, msg_dir, conflicts_dir, graph=tmp_path / "nope")

    assert result.returncode == 2
    assert result.stdout == ""


def test_missing_msg_dir_or_conflicts_dir_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    accepted = _accepted(tmp_path, "lode-a")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()

    result = _run(repo, accepted, tmp_path / "nope-msgs", conflicts_dir)
    assert result.returncode == 2
    assert result.stdout == ""

    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    result = _run(repo, accepted, msg_dir, tmp_path / "nope-conflicts")
    assert result.returncode == 2
    assert result.stdout == ""


def test_bad_invocation_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--accepted", str(tmp_path / "accepted")],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2

    result = subprocess.run(
        ["bash", str(SCRIPT), "--not-a-flag", "x"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2

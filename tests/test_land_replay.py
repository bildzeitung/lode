"""Tests for scripts/land-replay.sh (lode-s9xe.13).

`/land`'s Section 3 has TWO merge loops with the identical shape -- the
first-pass batch merge (scripts/land-merge-batch.sh, lode-s9xe.4) and this
isolation-replay copy, which runs after a `git reset --hard <base-ref>` and
gates EVERY branch individually so a red combined re-gate can be attributed
to a single culprit.

All tests below run the ACTUAL `scripts/land-replay.sh` against real git
repositories built in `tmp_path`, driving its real dependencies
(`land-merge-one.sh`, `drop-from-accepted.sh`, `land-state-load.sh`,
`assert-main-checkout.sh`) rather than mocking any of them -- the same
sabotage-provable bar tests/test_land_merge_batch.py uses. The one thing
that IS faked is `nox` itself: a tiny stand-in placed first on PATH, whose
verdict is driven by marker files committed into a branch, so a gate's
pass/fail is tied to the actual working tree content at the moment
land-replay.sh runs it -- exactly the property real `nox -s tests` has, and
exactly what lets these tests assert on the destructive `git reset --hard`
behavior around a bounced branch.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-replay.sh"

_FAKE_NOX = """#!/usr/bin/env bash
case "$1$2" in
  "-stests")
    if [ -f TESTS_FAULT_127 ]; then exit 127
    elif [ -f TESTS_FAIL ]; then exit 1
    else exit 0
    fi
    ;;
  "-tfix")
    if [ -f FIX_FAULT_127 ]; then exit 127
    elif [ -f FIX_FAIL ]; then exit 1
    else
      [ -f REFORMAT_ME ] && echo "reformatted" > reformat_target.txt
      exit 0
    fi
    ;;
  "-slock_currency")
    if [ -f LOCK_FAIL_2 ]; then exit 2
    elif [ -f LOCK_FAIL_1 ]; then exit 1
    else exit 0
    fi
    ;;
  *) echo "fake nox: unhandled args: $*" >&2; exit 3 ;;
esac
"""


def _fake_nox_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    nox = bin_dir / "nox"
    nox.write_text(_FAKE_NOX)
    nox.chmod(nox.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit on `trunk`, and an `origin/trunk`
    ref pointing at that same commit -- land-replay.sh's default --base-ref.

    `origin/land/<id>` branches are faked as plain local branches of that
    name, same as tests/test_land_merge_batch.py: land-merge-one.sh only
    ever does `git merge --no-ff origin/land/$id`, which needs a resolvable
    ref, not a configured remote.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("line1\nline2\nline3\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "origin/trunk", "trunk")
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
    landed: Path,
    *,
    graph: Path | None = None,
    base_ref: str | None = None,
    on_branch: str = "trunk",
    script: Path = SCRIPT,
    fake_nox: Path | None = None,
) -> subprocess.CompletedProcess:
    _git(repo, "checkout", "-q", on_branch)
    args = [
        "bash",
        str(script),
        "--accepted",
        str(accepted),
        "--msg-dir",
        str(msg_dir),
        "--conflicts-dir",
        str(conflicts_dir),
        "--landed",
        str(landed),
    ]
    if graph is not None:
        args += ["--graph", str(graph)]
    if base_ref is not None:
        args += ["--base-ref", base_ref]
    env = dict(os.environ)
    if fake_nox is not None:
        env["PATH"] = f"{fake_nox}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        args, cwd=repo, capture_output=True, text=True, timeout=30, check=False, env=env
    )


def test_two_clean_merges_both_land(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
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

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\nLANDED\tlode-b\n"
    assert landed.read_text() == "lode-a\nlode-b\n"
    assert accepted.read_text() == "lode-a\nlode-b\n"
    assert (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()


def test_landed_file_is_truncated_even_if_it_had_prior_content(
    tmp_path: Path,
) -> None:
    """The first-pass loop may have already recorded ids into --landed before
    the combined re-gate turned red and this replay reset the tree -- those
    records must not survive into this replay's own record."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"
    landed.write_text("lode-stale-from-first-pass\n")

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert landed.read_text() == "lode-a\n"


@pytest.mark.parametrize("sentinel", ["TESTS_FAIL", "FIX_FAIL"])
def test_a_branch_that_fails_a_nox_gate_is_bounced_and_backed_out(
    tmp_path: Path, sentinel: str
) -> None:
    """Exit 1 -- the one CONTENT verdict either per-branch nox gate has --
    still bounces. Parametrized over both gates since lode-lmu9 split `nox -t
    fix` and `nox -s tests` into separate arms: each arm now owns its own
    bounce path, so neither is covered by the other."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-good")
    _commit_file(repo, "good.txt", "fine\n", "good adds good.txt")
    _branch_from(repo, "trunk", "origin/land/lode-bad")
    _commit_file(repo, sentinel, "", "bad breaks a nox gate")
    _branch_from(repo, "trunk", "origin/land/lode-after")
    _commit_file(repo, "after.txt", "fine too\n", "after adds after.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (
        ("lode-good", "GOOD"),
        ("lode-bad", "BAD"),
        ("lode-after", "AFTER"),
    ):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-good", "lode-bad", "lode-after")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-good\nBOUNCED\tlode-bad\nLANDED\tlode-after\n"
    assert landed.read_text() == "lode-good\nlode-after\n"
    assert accepted.read_text() == "lode-good\nlode-after\n", (
        "the bounced id must be rewritten out of --accepted"
    )
    assert not (repo / sentinel).exists(), (
        "the bounce must back the bad merge out of the working tree"
    )
    assert (repo / "good.txt").exists()
    assert (repo / "after.txt").exists()
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""


def test_a_bounced_base_holds_its_dependent_too(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-base")
    _commit_file(repo, "TESTS_FAIL", "", "base breaks the tests")
    _branch_from(repo, "trunk", "origin/land/lode-dep")
    _commit_file(repo, "dep.txt", "from dep\n", "dep adds dep.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (("lode-base", "BASE"), ("lode-dep", "DEP")):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-base", "lode-dep")
    graph = _graph(tmp_path, ("lode-dep", "lode-base", "direct"))
    landed = tmp_path / "landed"

    result = _run(
        repo, accepted, msg_dir, conflicts_dir, landed, graph=graph, fake_nox=fake_nox
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines == ["BOUNCED\tlode-base", "HELD\tlode-dep"]
    assert accepted.read_text() == ""
    assert landed.read_text() == ""
    assert not (repo / "dep.txt").exists(), "a HELD id must never be merged"


def test_a_real_conflict_drops_the_id_and_continues(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
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
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\nCONFLICT\tlode-b\nLANDED\tlode-c\n"
    assert accepted.read_text() == "lode-a\nlode-c\n"
    assert landed.read_text() == "lode-a\nlode-c\n"
    assert (conflicts_dir / "lode-b").read_text() == "f.txt\n"
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""
    unmerged = _git(repo, "ls-files", "-u")
    assert unmerged.stdout == ""


def test_baseline_red_stops_before_merging_anything(tmp_path: Path) -> None:
    """`origin/trunk` itself carries the failing marker -- unattributable to
    any branch in --accepted. Nothing may be merged or bounced."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    (repo / "TESTS_FAIL").write_text("")
    _git(repo, "add", "TESTS_FAIL")
    _git(repo, "commit", "-q", "-m", "trunk itself is already red")
    _git(repo, "branch", "-f", "origin/trunk", "trunk")

    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "before any branch merged" in result.stderr
    assert not (repo / "a.txt").exists()
    # The reset ran BEFORE this stop, discarding whatever the first-pass loop
    # had merged -- so the durable record must not survive it still naming
    # merges that are no longer on the tree.
    assert landed.read_text() == ""


def test_an_unresolvable_base_ref_is_a_machine_fault(tmp_path: Path) -> None:
    """A failed `git reset --hard <base-ref>` must stop the pass, not fall
    through and attribute gates against whatever the first-pass loop left
    merged."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(
        repo,
        accepted,
        msg_dir,
        conflicts_dir,
        landed,
        base_ref="no-such-ref-anywhere",
        fake_nox=fake_nox,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "git reset --hard no-such-ref-anywhere" in result.stderr
    assert not (repo / "a.txt").exists(), "nothing may merge after a failed reset"


def test_baseline_lock_currency_machine_fault_stops_the_pass(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    (repo / "LOCK_FAIL_2").write_text("")
    _git(repo, "add", "LOCK_FAIL_2")
    _git(repo, "commit", "-q", "-m", "trunk's lock_currency machine-faults")
    _git(repo, "branch", "-f", "origin/trunk", "trunk")

    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "machine-faulted" in result.stderr


def test_mid_loop_lock_currency_machine_fault_stops_the_pass(tmp_path: Path) -> None:
    """A later id's merge makes `nox -s lock_currency` machine-fault: this is
    NOT that branch's verdict, and must never be read as a bounce."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    _branch_from(repo, "trunk", "origin/land/lode-b")
    _commit_file(repo, "LOCK_FAIL_2", "", "b's merge makes lock_currency fault")
    _branch_from(repo, "trunk", "origin/land/lode-c")
    _commit_file(repo, "c.txt", "from C\n", "C adds c.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (("lode-a", "A"), ("lode-b", "B"), ("lode-c", "C")):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a", "lode-b", "lode-c")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\n"
    assert landed.read_text() == "lode-a\n"
    assert "BOUNCED" not in result.stdout
    assert not (repo / "c.txt").exists(), "an id after the fault must never be merged"


@pytest.mark.parametrize(
    ("sentinel", "gate"),
    [("FIX_FAULT_127", "nox -t fix"), ("TESTS_FAULT_127", "nox -s tests")],
)
def test_mid_loop_nonverdict_nox_exit_stops_the_pass_without_bouncing(
    tmp_path: Path, sentinel: str, gate: str
) -> None:
    """A 127 (nox not on PATH mid-run) from EITHER per-branch nox gate after a
    clean merge is a machine fault, not that id's verdict -- it must stop the
    replay, never bounce the branch that happened to be merged when it hit
    (lode-lmu9). Both gates are parametrized here rather than written twice:
    they are two arms of one contract, and a change to one that is not
    mirrored in the other is exactly what this pins."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    _branch_from(repo, "trunk", "origin/land/lode-b")
    _commit_file(repo, sentinel, "", f"b's merge makes {gate} fault")
    _branch_from(repo, "trunk", "origin/land/lode-c")
    _commit_file(repo, "c.txt", "from C\n", "C adds c.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (("lode-a", "A"), ("lode-b", "B"), ("lode-c", "C")):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a", "lode-b", "lode-c")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\n"
    assert landed.read_text() == "lode-a\n"
    assert "BOUNCED" not in result.stdout
    assert "machine fault" in result.stderr
    assert not (repo / "c.txt").exists(), "an id after the fault must never be merged"


def test_landed_reformat_is_committed_as_part_of_the_merge(tmp_path: Path) -> None:
    """`nox -t fix` reformatting the just-merged content must be folded into
    the merge commit (not left dirty) so the NEXT iteration's merge meets a
    clean tree, and a later bounce's single `git reset --hard HEAD~1` would
    discard both together (lode-lmu9)."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    _commit_file(
        repo, "reformat_target.txt", "unformatted\n", "A adds an unformatted file"
    )
    _commit_file(repo, "REFORMAT_ME", "", "trigger the fake reformat")
    _branch_from(repo, "trunk", "origin/land/lode-b")
    _commit_file(repo, "b.txt", "from B\n", "B adds b.txt")

    msg_dir = tmp_path / "msgs"
    for id_, label in (("lode-a", "A"), ("lode-b", "B")):
        _write_msg(msg_dir, id_, f"Merge land/{id_}: {label} ({id_})")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a", "lode-b")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\nLANDED\tlode-b\n"
    assert landed.read_text() == "lode-a\nlode-b\n"
    # The reformat landed, and the tree is clean -- no separate uncommitted
    # reformat left behind for the next merge (lode-b's) to trip over.
    assert (repo / "reformat_target.txt").read_text() == "reformatted\n"
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""
    # b.txt must still exist -- proof the second merge succeeded against a
    # clean tree rather than machine-faulting on a's leftover dirt.
    assert (repo / "b.txt").exists()
    # The reformat is IN lode-a's own merge commit (--amend), not a
    # trailing, separately-authored commit on top of it.
    merge_sha = _git(
        repo, "log", "--format=%H", "--grep=Merge land/lode-a", "origin/trunk..HEAD"
    ).stdout.strip()
    assert merge_sha, "expected to find lode-a's merge commit"
    content_at_merge = _git(repo, "show", f"{merge_sha}:reformat_target.txt").stdout
    assert content_at_merge == "reformatted\n"
    # And no separately-authored "style:"/reformat commit exists on top.
    log = _git(repo, "log", "--oneline", "origin/trunk..HEAD")
    assert "style" not in log.stdout.lower()


def test_an_unrunnable_land_merge_one_is_a_fault_not_a_conflict(
    tmp_path: Path,
) -> None:
    """A bootstrap gap must never read as a branch verdict -- same defect
    class as tests/test_land_merge_batch.py's identically-named test."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    scripts = tmp_path / "scripts"
    shutil.copytree(REPO_ROOT / "scripts", scripts)
    (scripts / "land-merge-one.sh").chmod(0o644)

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(
        repo,
        accepted,
        msg_dir,
        conflicts_dir,
        landed,
        script=scripts / "land-replay.sh",
        fake_nox=fake_nox,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "CONFLICT" not in result.stdout
    assert not (conflicts_dir / "lode-a").exists()
    assert accepted.read_text() == "lode-a\n", (
        "a machine fault must not drop the id from the accepted set"
    )


def test_missing_accepted_file_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    landed = tmp_path / "landed"

    result = _run(
        repo, tmp_path / "nope", msg_dir, conflicts_dir, landed, fake_nox=fake_nox
    )

    assert result.returncode == 2
    assert result.stdout == ""


def test_empty_accepted_file_is_a_machine_fault(tmp_path: Path) -> None:
    """Unlike land-merge-batch.sh, an EMPTY accepted set here is always a
    fault (lode-0jan's asymmetry preserved from the isolation-replay path's
    own SKILL.md prose): this script only runs after a combined re-gate
    turned red, and a nothing-merged pass skips that re-gate -- and this
    script -- entirely, so an empty set at this point is unreachable and
    never a legitimate outcome."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = tmp_path / "accepted"
    accepted.write_text("")
    landed = tmp_path / "landed"

    result = _run(repo, accepted, msg_dir, conflicts_dir, landed, fake_nox=fake_nox)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""


def test_missing_graph_file_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(
        repo,
        accepted,
        msg_dir,
        conflicts_dir,
        landed,
        graph=tmp_path / "nope",
        fake_nox=fake_nox,
    )

    assert result.returncode == 2
    assert result.stdout == ""


def test_missing_msg_dir_or_conflicts_dir_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    accepted = _accepted(tmp_path, "lode-a")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    landed = tmp_path / "landed"

    result = _run(
        repo, accepted, tmp_path / "nope-msgs", conflicts_dir, landed, fake_nox=fake_nox
    )
    assert result.returncode == 2
    assert result.stdout == ""

    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    result = _run(
        repo, accepted, msg_dir, tmp_path / "nope-conflicts", landed, fake_nox=fake_nox
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_missing_landed_argument_is_a_machine_fault(tmp_path: Path) -> None:
    """Unlike land-merge-batch.sh's optional --landed, this script requires
    it -- it is the whole record this loop exists to produce."""
    repo = _init_repo(tmp_path)
    accepted = _accepted(tmp_path, "lode-a")
    msg_dir = tmp_path / "msgs"
    msg_dir.mkdir()
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--accepted",
            str(accepted),
            "--msg-dir",
            str(msg_dir),
            "--conflicts-dir",
            str(conflicts_dir),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
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


def test_not_the_main_checkout_is_a_machine_fault(tmp_path: Path) -> None:
    """A linked worktree of the throwaway repo must be refused before any
    destructive git call runs -- same guard land-merge-one.sh asserts
    internally (lode-1nty), asserted here as this script's own first action
    since it performs its own `git reset --hard` calls."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    _branch_from(repo, "trunk", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")
    _git(repo, "checkout", "-q", "trunk")

    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(worktree), "trunk")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(
        worktree,
        accepted,
        msg_dir,
        conflicts_dir,
        landed,
        on_branch="side",
        fake_nox=fake_nox,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "NOT RUNNING IN THE MAIN CHECKOUT" in result.stderr
    assert result.stdout == ""


def test_explicit_base_ref_is_honored(tmp_path: Path) -> None:
    """--base-ref overrides the origin/trunk default -- the reset and the
    baseline gates run against whatever ref is named."""
    repo = _init_repo(tmp_path)
    fake_nox = _fake_nox_bin(tmp_path)
    # A second base, deliberately NOT origin/trunk, one commit ahead.
    _git(repo, "checkout", "-q", "trunk")
    _git(repo, "checkout", "-q", "-b", "other-base")
    _commit_file(repo, "only-on-other-base.txt", "x\n", "other-base's own commit")

    _branch_from(repo, "other-base", "origin/land/lode-a")
    _commit_file(repo, "a.txt", "from A\n", "A adds a.txt")

    msg_dir = tmp_path / "msgs"
    _write_msg(msg_dir, "lode-a", "Merge land/lode-a: A (lode-a)")
    conflicts_dir = tmp_path / "conflicts"
    conflicts_dir.mkdir()
    accepted = _accepted(tmp_path, "lode-a")
    landed = tmp_path / "landed"

    result = _run(
        repo,
        accepted,
        msg_dir,
        conflicts_dir,
        landed,
        base_ref="other-base",
        on_branch="other-base",
        fake_nox=fake_nox,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "LANDED\tlode-a\n"
    assert (repo / "only-on-other-base.txt").exists()
    assert (repo / "a.txt").exists()

"""Tests for scripts/land-lock.sh (lode-aps3).

`/land`'s Section 0 (`.claude/skills/land/SKILL.md`) is supposed to serialize
`/land` passes on one machine with a local lockfile: only one tick may hold
it at a time, and the rest skip cleanly rather than overlapping the one
agent allowed to write `trunk`. The inline snippet this script replaces
relied on `trap 'rm -f "$LOCK"' EXIT` to release the lock at pass end -- but
an agent running the skill executes each fenced `bash` block as its own,
separate Bash tool invocation, so that trap fired the instant Section 0's
own block ended, releasing the lock before Section 1 even ran. Worse, its
stale-lock reclaim compared the recorded PID with `kill -0`, and a PID
recorded by ANY earlier Bash invocation is *always* already dead by the time
a later invocation reads it in this per-block architecture -- so PID
liveness could never tell "still running, just between blocks" apart from
"crashed". VERIFIED LIVE (bd show lode-aps3's notes, 2026-07-27, real /land
pass on trunk @ d732b05): the lock was gone by the second of two separate
Bash invocations.

The fix drops the trap and the PID check entirely in favor of a wall-clock
staleness token: the lock records when it was acquired, and a later
`acquire` reclaims it only once that recorded time is older than
`LAND_LOCK_STALE_SECONDS` (default 1800s). This is the sole mechanism
guaranteed to release an abandoned lock -- SKILL.md calls `release`
explicitly only at the two points a normal pass is guaranteed to reach, and
every other exit relies on this staleness reclaim.

All tests below run the ACTUAL `scripts/land-lock.sh` against a real,
throwaway git repository in `tmp_path` (the script's only external
dependency is `git rev-parse --git-dir`, to place the lock file under
`.git/`) -- no fake git, no mocked subprocess, so a regression to the script
itself turns these red.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-lock.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo -- just enough for `git rev-parse --git-dir`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "trunk"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return repo


def _lock_path(repo: Path) -> Path:
    return repo / ".git" / "land.lock"


def _run(
    *args: str, repo: Path, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


def test_acquire_on_a_fresh_repo_succeeds_and_writes_a_lock_file(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)

    result = _run("acquire", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    lock = _lock_path(repo)
    assert lock.exists()
    fields = lock.read_text().split()
    assert len(fields) == 4, fields  # pid hostname epoch iso8601
    assert fields[0].isdigit()  # pid, recorded for humans only (see header)
    assert fields[2].isdigit()  # epoch seconds -- the ONLY field acquire reads back


def test_second_acquire_without_release_is_skipped_while_fresh(
    tmp_path: Path,
) -> None:
    """This is the core defect: a lock just acquired must still be held for
    a SEPARATE, later invocation -- exactly the cross-Bash-call scenario the
    old trap-based design got wrong (it released before the next invocation
    even ran). Two separate `subprocess.run` calls stand in for two separate
    Bash tool invocations."""
    repo = _init_repo(tmp_path)
    first = _run("acquire", repo=repo)
    assert first.returncode == 0, first.stdout + first.stderr

    second = _run("acquire", repo=repo)

    assert second.returncode == 1, second.stdout + second.stderr
    assert "skipping this tick" in second.stderr
    # The lock is untouched -- still the FIRST acquire's record, not reclaimed.
    lock = _lock_path(repo)
    assert lock.exists()


def test_release_then_acquire_succeeds_again(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert _run("acquire", repo=repo).returncode == 0

    released = _run("release", repo=repo)
    assert released.returncode == 0
    assert not _lock_path(repo).exists()

    reacquired = _run("acquire", repo=repo)
    assert reacquired.returncode == 0, reacquired.stdout + reacquired.stderr


def test_release_with_no_lock_held_is_a_harmless_no_op(tmp_path: Path) -> None:
    """A caller must be able to call `release` even on a path where it never
    held the lock (e.g. it just skipped the tick) without that erroring."""
    repo = _init_repo(tmp_path)

    result = _run("release", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _lock_path(repo).exists()


# ---------------------------------------------------------------------------
# Staleness reclaim -- the mechanism that replaces the dead-PID trap logic
# ---------------------------------------------------------------------------


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    """A lock recorded well past the staleness threshold is treated as an
    abandoned prior pass and reclaimed -- the self-healing half of the fix."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"12345 some-old-host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "500"}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reclaiming stale lock" in result.stdout
    # The lock file now reflects the NEW acquisition, not the stale one.
    new_fields = lock.read_text().split()
    assert int(new_fields[2]) > old_epoch


def test_fresh_lock_is_not_reclaimed_even_with_a_short_threshold_boundary(
    tmp_path: Path,
) -> None:
    """A lock younger than the threshold must never be reclaimed, however
    small the threshold -- staleness is a strict `>=` comparison, not a
    guess."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    recent_epoch = int(time.time())
    lock.write_text(f"12345 host {recent_epoch} 2026-01-01T00:00:00Z\n")

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "3600"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    # Untouched: still the original epoch.
    assert lock.read_text().split()[2] == str(recent_epoch)


def test_fresh_lock_with_an_unreachable_pid_is_not_reclaimed(tmp_path: Path) -> None:
    """Regression pin: a future edit reintroducing `kill -0 $OWNER_PID`
    liveness would reclaim this lock immediately, because PID 999999999
    almost certainly does not exist on any test machine -- even though the
    recorded timestamp is fresh. Liveness must be judged ONLY by the
    timestamp, never the pid field."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    recent_epoch = int(time.time())
    lock.write_text(f"999999999 some-host {recent_epoch} 2026-01-01T00:00:00Z\n")

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "skipping this tick" in result.stderr


def test_malformed_lock_file_is_treated_as_still_held_not_reclaimed(
    tmp_path: Path,
) -> None:
    """A lock file that doesn't parse (no numeric epoch in the 3rd field) is
    age-unknown, not age-zero and not age-infinite -- the conservative
    reading is "still held", even under a threshold of 0, which would
    reclaim anything with a legible timestamp instantly."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    lock.write_text("this is not a lock record\n")

    result = _run("acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "0"})

    assert result.returncode == 1, result.stdout + result.stderr
    # Left exactly as it was -- never rewritten out from under an ambiguous read.
    assert lock.read_text() == "this is not a lock record\n"


def test_default_staleness_threshold_is_generous(tmp_path: Path) -> None:
    """No env override: a lock a couple of minutes old must NOT be reclaimed
    by the default threshold (1800s) -- a genuinely still-running /land pass
    (dispatching several land-review subagents, then a combined re-gate) can
    easily take a few minutes between two Bash invocations."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    two_minutes_ago = int(time.time()) - 120
    lock.write_text(f"12345 host {two_minutes_ago} 2026-01-01T00:00:00Z\n")

    result = _run("acquire", repo=repo)

    assert result.returncode == 1, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Usage errors
# ---------------------------------------------------------------------------


def test_no_args_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run(repo=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


def test_unknown_subcommand_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run("status", repo=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


# ---------------------------------------------------------------------------
# Lock placement
# ---------------------------------------------------------------------------


def test_lock_lives_under_git_dir(tmp_path: Path) -> None:
    """Same path convention the inline snippet this replaces used: per-machine,
    under `.git/`, never committed."""
    repo = _init_repo(tmp_path)

    assert _run("acquire", repo=repo).returncode == 0

    assert _lock_path(repo).exists()

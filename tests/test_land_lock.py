"""Tests for scripts/land-lock.sh (lode-aps3).

WHY the trap-and-PID design this replaces could not work -- an agent runs
each fenced `bash` block of `.claude/skills/land/SKILL.md` as its own Bash
invocation, so a `trap ... EXIT` fires before the next section runs and a
recorded PID is *always* already dead -- and why the replacement is a
wall-clock staleness token: see the header comment of scripts/land-lock.sh.
That rationale is deliberately NOT restated here (the
tests/test_blocks_dependents.py precedent) -- it lives next to the code it
constrains, so it cannot drift out of sync with a second copy. The header
also records the mechanism's known limits (the stale-lock reclaim path is
not atomic, CAVEAT 2) and the `heartbeat` subcommand (lode-m87j) that turns
the TTL from acquisition-age into idle-time semantics, CAVEAT 1.

What this file adds on top of that is the regression gate, in two halves:

1. Behavioural tests that run the ACTUAL script against a real, throwaway
   git repository in `tmp_path` (its only external dependency is
   `git rev-parse --git-dir`, to place the lock under `.git/`) -- no fake
   git, no mocked subprocess. Reintroducing either half of the old design
   turns these red: a `trap` release makes
   `test_second_acquire_without_release_is_skipped_while_fresh` fail (the
   lock is gone by the next invocation), and a `kill -0` liveness check
   makes `test_fresh_lock_with_an_unreachable_pid_is_not_reclaimed` fail.
   Both mutations were run against this file and confirmed red (5 and 6
   failures of 14 respectively).

2. Call-site pins against the SHIPPED `SKILL.md`. The defect lived in a
   markdown fence, where no gate reaches it, so the behavioural tests above
   would all stay green while SKILL.md quietly went back to an inline trap
   and stopped calling this script at all. Same reasoning and same shape as
   `tests/test_isolation_guard.py`'s `test_every_agent_definition_invokes_
   the_guard`.
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
    # Placement matters as much as content: under `.git/`, so it is
    # per-machine and can never be committed (same path the inline snippet
    # this replaces used).
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
# Heartbeat -- turns the TTL from acquisition-age into idle-time (lode-m87j)
# ---------------------------------------------------------------------------


def test_heartbeat_refreshes_an_existing_locks_timestamp(tmp_path: Path) -> None:
    """The core new behaviour: a lock this pass already holds gets a fresh
    epoch on every heartbeat call, without needing to go through `acquire`
    again (which would just fail -- the lock is still fresh)."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run("heartbeat", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    new_epoch = int(lock.read_text().split()[2])
    assert new_epoch > old_epoch


def test_heartbeat_on_a_missing_lock_creates_one(tmp_path: Path) -> None:
    """Heartbeat is unconditional -- if the lock file is somehow already gone
    (should not happen at either documented call site, but must not crash the
    pass if it does), it creates a fresh one rather than erroring, since the
    caller's intent ("the pass is still alive right now") is the same either
    way as a normal refresh."""
    repo = _init_repo(tmp_path)
    assert not _lock_path(repo).exists()

    result = _run("heartbeat", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lock_path(repo).exists()


def test_stale_lock_refreshed_by_heartbeat_is_not_reclaimed(tmp_path: Path) -> None:
    """The regression this subcommand exists to prevent: a lock old enough to
    be judged stale under the TTL must NOT be reclaimed by a later `acquire`
    once a `heartbeat` in between has re-stamped it fresh -- i.e. the TTL
    genuinely measures idle time now, not the original acquire's age."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    heartbeat = _run(
        "heartbeat", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "500"}
    )
    assert heartbeat.returncode == 0, heartbeat.stdout + heartbeat.stderr

    # Without the heartbeat above, this acquire would reclaim the lock (its
    # original 1000s age exceeds the 500s threshold) -- see
    # test_stale_lock_is_reclaimed below for that baseline behaviour.
    second_acquire = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "500"}
    )

    assert second_acquire.returncode == 1, second_acquire.stdout + second_acquire.stderr
    assert "skipping this tick" in second_acquire.stderr


def test_heartbeat_write_failure_is_reported_but_never_crashes(
    tmp_path: Path,
) -> None:
    """A heartbeat that cannot write (unwritable git dir) must exit 1 with a
    clear diagnostic -- never a silent success, and never an uncaught
    failure that could be mistaken for a script bug (lode-aps3's own "lock
    was not held must be observable, never silent" standard, applied here to
    the write path instead of the read path)."""
    repo = _init_repo(tmp_path)
    git_dir = repo / ".git"
    original_mode = git_dir.stat().st_mode
    git_dir.chmod(0o500)  # readable + traversable, not writable
    try:
        result = _run("heartbeat", repo=repo)
    finally:
        git_dir.chmod(original_mode)  # or tmp_path teardown fails

    assert result.returncode == 1, result.stdout + result.stderr
    assert "heartbeat could not write" in result.stderr
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


def test_fresh_lock_is_not_reclaimed_under_a_large_threshold(
    tmp_path: Path,
) -> None:
    """A lock younger than the threshold must never be reclaimed. (This is
    not a boundary test: pinning `>=` against `>` would mean asserting on a
    one-second window that `date` can cross mid-test, and the two differ by
    one second on a 1800s default -- operationally nothing.)"""
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
# Machine fault vs. "another lander"
# ---------------------------------------------------------------------------


def test_uncreatable_lock_reports_a_machine_fault_not_another_lander(
    tmp_path: Path,
) -> None:
    """`write_lock` discards its own stderr, so a lock that cannot be created
    at all (unwritable git dir, full disk) is indistinguishable from one that
    already exists unless the script checks. Reporting it as "another /land is
    running" would block landing indefinitely behind a lander that does not
    exist, every tick, with nothing naming the real cause -- lode-aps3's own
    notes require "lock was not held" to be observable rather than silent."""
    repo = _init_repo(tmp_path)
    git_dir = repo / ".git"
    original_mode = git_dir.stat().st_mode
    git_dir.chmod(0o500)  # readable + traversable, not writable
    try:
        result = _run("acquire", repo=repo)
    finally:
        git_dir.chmod(original_mode)  # or tmp_path teardown fails

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    assert "another /land" not in result.stderr
    assert not _lock_path(repo).exists()


# ---------------------------------------------------------------------------
# Call-site pins against the SHIPPED SKILL.md (the fence is where the bug was)
# ---------------------------------------------------------------------------

LAND_SKILL = REPO_ROOT / ".claude" / "skills" / "land" / "SKILL.md"


def test_land_skill_acquires_and_releases_through_this_script() -> None:
    """The lock only serializes a pass that actually calls this script, and
    every call site is prose in a markdown fence that no gate parses. Pin the
    shipped file (the tests/_hookharness.py precedent: assert what is
    committed, never a reimplementation)."""
    text = LAND_SKILL.read_text(encoding="utf-8")

    assert "scripts/land-lock.sh acquire" in text, (
        "land/SKILL.md never acquires the single-lander lock -- overlapping "
        "/loop 5m /land ticks would both write trunk (lode-aps3)"
    )
    # Both explicit release sites: Section 1's empty-queue exit and the end of
    # Section 4. Losing one is not a correctness bug (the TTL still reclaims)
    # but it silently costs up to LAND_LOCK_STALE_SECONDS of blocked landing.
    assert text.count("scripts/land-lock.sh release") >= 2, (
        "land/SKILL.md lost one of its two explicit `release` call sites -- "
        "that pass now waits out the whole staleness window instead"
    )


def test_land_skill_heartbeats_the_lock_once_per_ticket_in_section_2a() -> None:
    """lode-m87j: the vet loop (Section 2a) must heartbeat the lock as the
    FIRST action of every iteration, so the staleness window measures the gap
    since the last ticket's dispatch rather than the whole pass's duration.
    A dropped call site here silently reintroduces the acquisition-age
    exposure this ticket exists to close -- pinned the same way acquire and
    release are above."""
    text = LAND_SKILL.read_text(encoding="utf-8")

    assert "scripts/land-lock.sh heartbeat" in text, (
        "land/SKILL.md never heartbeats the single-lander lock -- the TTL is "
        "back to measuring acquisition age, not idle time (lode-m87j)"
    )


def _fenced_bash(markdown: str) -> str:
    """The ```bash fences only -- what an agent actually EXECUTES.

    Scanning the whole file would match the prose that *explains* the old
    defect (it necessarily quotes `trap` and `kill -0`), so the pin has to
    separate what is executed from what is merely described. That split is
    also the point: the fence is the one part of this skill no gate parses,
    which is how the bug survived unnoticed in the first place.
    """
    blocks: list[str] = []
    in_bash = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            if in_bash:
                in_bash = False
            else:
                in_bash = line.strip() in {"```bash", "```sh"}
            continue
        if in_bash:
            blocks.append(line)
    return "\n".join(blocks)


def test_land_skill_never_reintroduces_an_inline_lock() -> None:
    """The exact regression this ticket fixed: a lock managed inline in a
    fenced block via `trap`/`kill -0`/`noclobber`, none of which can outlive
    the single Bash invocation that block runs in."""
    executed = _fenced_bash(LAND_SKILL.read_text(encoding="utf-8"))
    assert "land-lock.sh acquire" in executed, (
        "the acquire call is not inside an executable ```bash fence -- "
        "_fenced_bash() or the skill's layout has drifted"
    )
    assert "land-lock.sh heartbeat" in executed, (
        "the heartbeat call (Section 2a) is not inside an executable ```bash "
        "fence -- test_land_skill_heartbeats_the_lock_once_per_ticket_in_"
        "section_2a found it in the file's prose but not where it is actually "
        "EXECUTED (lode-m87j)"
    )

    offenders = [
        pattern for pattern in ("trap ", "kill -0", "noclobber") if pattern in executed
    ]
    assert not offenders, (
        f"land/SKILL.md EXECUTES inline lock machinery {offenders} -- a trap dies "
        "with its own fenced block and a recorded PID is always already dead by "
        "the next one; the lock must go through scripts/land-lock.sh (lode-aps3)"
    )

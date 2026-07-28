"""Tests for scripts/land-lock.sh (lode-aps3, lode-ao95).

WHY the trap-and-PID design this replaces could not work -- an agent runs
each fenced `bash` block of `.claude/skills/land/SKILL.md` as its own Bash
invocation, so a `trap ... EXIT` fires before the next section runs and a
recorded PID is *always* already dead -- and why the replacement is a
wall-clock staleness token: see the header comment of scripts/land-lock.sh.
That rationale is deliberately NOT restated here (the
tests/test_blocks_dependents.py precedent) -- it lives next to the code it
constrains, so it cannot drift out of sync with a second copy. The header
also records the one remaining known limit of the mechanism (the TTL
measures acquisition age, not idle time) and the deliberately-deferred gap
(an ownership check in a future `heartbeat`, lode-q9pm) -- the reclaim path
itself is no longer non-atomic; see lode-ao95's half of the tests below.

What this file adds on top of that is the regression gate, in three parts:

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

2. A concurrency stress test (lode-ao95) reproducing the actual defect: many
   rounds of N-way concurrent `acquire` calls against the SAME manually
   crafted stale lock, asserting exactly one winner per round. Run against
   the pre-fix script (`rm` then create, two steps) this reliably shows
   multiple winners in some rounds; against the fixed script it must never
   show more than one, in any round. The CONTENTION LEVEL is part of the
   gate, not a free parameter -- 8-way reproduces the original two-step
   defect but is blind to the narrower re-validation one that replaced it.
   See that test's own docstring for the measurements.

3. Call-site pins against the SHIPPED `SKILL.md`. The defect lived in a
   markdown fence, where no gate reaches it, so the behavioural tests above
   would all stay green while SKILL.md quietly went back to an inline trap
   and stopped calling this script at all. Same reasoning and same shape as
   `tests/test_isolation_guard.py`'s `test_every_agent_definition_invokes_
   the_guard`.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import threading
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


def _gate_path(repo: Path) -> Path:
    """The reclaim gate, derived the same way the script derives it
    (`RECLAIM_GATE="$LOCK.reclaiming"`) rather than hand-spelled -- so a
    rename of the lock file cannot leave the gate tests pointing at a path
    nothing creates any more, which would turn them green and vacuous."""
    return _lock_path(repo).with_name(_lock_path(repo).name + ".reclaiming")


def _write_stale_lock(repo: Path, *, age: int = 100_000) -> int:
    """A lock record far past any plausible staleness threshold. Deliberately
    FOUR fields: an old record predating the owner token must still parse, and
    that back-compatibility is worth exercising rather than assuming."""
    old_epoch = int(time.time()) - age
    _lock_path(repo).write_text(
        f"12345 some-old-host {old_epoch} 2020-01-01T00:00:00Z\n"
    )
    return old_epoch


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
    assert len(fields) == 5, fields  # pid hostname epoch iso8601 owner-token
    assert fields[0].isdigit()  # pid, recorded for humans only (see header)
    assert fields[2].isdigit()  # epoch seconds -- the ONLY field acquire reads back
    # Owner token (lode-ao95): opaque, non-empty, distinct across acquisitions
    # -- not read back by anything in THIS script yet (see CAVEAT 2 / lode-q9pm)
    # but must actually be present and vary, or a future ownership check has
    # nothing to compare against.
    assert fields[4], fields
    second = _run("release", repo=repo)
    assert second.returncode == 0
    reacquired = _run("acquire", repo=repo)
    assert reacquired.returncode == 0
    assert lock.read_text().split()[4] != fields[4], (
        "two separate acquisitions produced the SAME owner token"
    )


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
# Atomic reclaim (lode-ao95) -- the mkdir-gated fix for the two-winner race
# ---------------------------------------------------------------------------


def test_concurrent_acquire_against_a_stale_lock_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    """The regression this ticket fixes: two concurrent `acquire` calls both
    exiting 0 against one stale lock. Reproduce the race, then assert every
    round has EXACTLY one winner.

    CONTENTION IS LOAD-BEARING -- do not lower it. 8-way (where the original
    `rm`-then-create defect was first measured) is NOT enough to cover the
    fixed code: the gate closed the two-step race, but left a second,
    narrower one in the gate-winner's re-validation, and that one is
    invisible at 8 workers. Measured on the same machine, same harness:

        8-way,  200 rounds -> 0 multi-winner   (the bug is INVISIBLE here)
        32-way,  60 rounds -> 11 multi-winner  (~18%/round)

    So a version of this test at 8-way would have passed against code that
    still admitted two landers onto `trunk`. 32 workers x 40 rounds makes a
    regression a near-certainty to surface (P(miss) ~ 0.8^40) and costs a
    few seconds. The assertion is `== 1`, not `<= 1`, deliberately: a "fix"
    that simply blocked every racer would wedge landing outright while
    satisfying any check that only counted upwards.
    """
    repo = _init_repo(tmp_path)
    gate = _gate_path(repo)
    rounds = 40
    workers = 32
    # Hoisted, not per-round: a Barrier resets itself once all `workers`
    # parties have tripped it, and every round trips it exactly once with
    # exactly that many.
    barrier = threading.Barrier(workers)

    def _race() -> subprocess.CompletedProcess:
        barrier.wait(timeout=30)
        return _run(
            "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
        )

    for round_num in range(rounds):
        # Reset BOTH pieces of state between rounds: a gate left behind by the
        # previous round would make the next one skip instead of race, quietly
        # reducing the effective sample size rather than failing.
        shutil.rmtree(gate, ignore_errors=True)
        _write_stale_lock(repo)

        # Release every racer from the same barrier. Without it each thread
        # reaches its own `subprocess.run` whenever the GIL and the fork storm
        # let it, which staggers the starts by milliseconds -- enough to
        # thin the interleaving badly: measured against a deliberately
        # reverted re-validation, the un-barriered version detected the
        # regression in only 4 of 6 runs, and a ~30% miss rate on a
        # two-lander bug is not a gate. With the barrier it is 10 of 10.
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_race) for _ in range(workers)]
            results = [f.result() for f in futures]

        winners = [r for r in results if r.returncode == 0]
        assert len(winners) == 1, (
            f"round {round_num}: {len(winners)} winners (expected exactly 1)\n"
            + "\n".join(
                f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}" for r in results
            )
        )


def test_a_gate_busy_with_a_live_reclaim_is_not_treated_as_abandoned(
    tmp_path: Path,
) -> None:
    """A freshly-created reclaim gate (age well under
    RECLAIM_GATE_STALE_SECONDS) must block a concurrent acquire outright --
    it must NOT be cleared and retried, since a genuine reclaim could still
    be in flight."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = _write_stale_lock(repo)

    gate = _gate_path(repo)
    gate.mkdir()
    (gate / "created").write_text(str(int(time.time())))

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    # Untouched: the gate holder (simulated here) is still the only one
    # allowed to reclaim; the main lock file is exactly as it was.
    assert lock.read_text().split()[2] == str(old_epoch)
    assert gate.exists()


def test_a_gate_with_no_creation_stamp_is_dated_rather_than_wedging(
    tmp_path: Path,
) -> None:
    """A reclaimer killed between `mkdir "$LOCK.reclaiming"` and writing the
    stamp inside it leaves a gate with no `created` file; treating that as
    "still in progress" and skipping wedges landing PERMANENTLY, since
    nothing else ever removes a gate and the abandoned-gate branch needs a
    timestamp to age one out. The full argument is at the code it constrains
    (scripts/land-lock.sh, the gate-taken branch).

    So the acquire that finds an unstamped gate must DATE it. This test pins
    that half; `test_an_abandoned_reclaim_gate_is_cleared_and_retried` pins
    the other half (a dated gate does get cleared once past the window), and
    together they are "no permanent wedge" -- without needing a 30s sleep in
    the suite to observe it end-to-end.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = _write_stale_lock(repo)

    gate = _gate_path(repo)
    gate.mkdir()  # no `created` inside: killed between mkdir and the stamp
    assert not (gate / "created").exists()

    before = int(time.time())
    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    # This tick still skips -- it cannot know the gate is dead rather than
    # microseconds old. What matters is that it left the gate DATABLE.
    assert result.returncode == 1, result.stdout + result.stderr
    stamp = (gate / "created").read_text().strip()
    assert stamp.isdigit(), f"gate left undatable, landing wedges: {stamp!r}"
    assert int(stamp) >= before
    # The stale lock itself is untouched -- stamping is not reclaiming.
    assert lock.read_text().split()[2] == str(old_epoch)


def test_an_abandoned_reclaim_gate_is_cleared_and_retried(tmp_path: Path) -> None:
    """The no-wedge half of the fix: a reclaim gate left behind by a
    reclaimer that crashed between winning it and clearing it (age well past
    RECLAIM_GATE_STALE_SECONDS) must NOT block landing forever -- a later
    acquire clears it and retries, successfully reclaiming the still-stale
    main lock."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = _write_stale_lock(repo)

    gate = _gate_path(repo)
    gate.mkdir()
    (gate / "created").write_text(str(int(time.time()) - 1000))  # long abandoned

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not gate.exists(), "the abandoned gate must be cleared, not left behind"
    new_fields = lock.read_text().split()
    assert int(new_fields[2]) > old_epoch


# NOTE on the gate-winner's internal re-validation (land-lock.sh's own
# comment: "Re-validate before touching $LOCK"). There is deliberately no
# SEPARATE deterministic unit test for it. The interleaving it guards --
# this racer's own pre-loop staleness check sees STALE, and by the time it
# wins the gate another racer's reclaim is already in flight or complete --
# cannot be staged from a single sequential invocation without mocking the
# script's internals, which the rest of this file avoids on purpose (see the
# module docstring). It is covered by the stress test above instead.
#
# That coverage is only as good as the CONTENTION, which is why `workers` is
# pinned rather than left to taste -- measurements and the full argument are
# in that test's own docstring. Lowering it to speed this file up deletes the
# only gate that has ever caught a live two-lander bug in this script.


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

    offenders = [
        pattern for pattern in ("trap ", "kill -0", "noclobber") if pattern in executed
    ]
    assert not offenders, (
        f"land/SKILL.md EXECUTES inline lock machinery {offenders} -- a trap dies "
        "with its own fenced block and a recorded PID is always already dead by "
        "the next one; the lock must go through scripts/land-lock.sh (lode-aps3)"
    )

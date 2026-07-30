"""Tests for scripts/land-lock.sh (lode-aps3, lode-ao95).

WHY the trap-and-PID design this replaces could not work -- an agent runs
each fenced `bash` block of `.claude/skills/land/SKILL.md` as its own Bash
invocation, so a `trap ... EXIT` fires before the next section runs and a
recorded PID is *always* already dead -- and why the replacement is a
wall-clock staleness token: see the header comment of scripts/land-lock.sh.
That rationale is deliberately NOT restated here (the
tests/test_blocks_dependents.py precedent) -- it lives next to the code it
constrains, so it cannot drift out of sync with a second copy. The header
also records the mechanism's known limits: the `heartbeat` subcommand
(lode-m87j) moves the TTL toward idle-time semantics over the two loops it
brackets -- but not over the whole pass; CAVEAT 1 enumerates the three
stretches that stay uncovered and why the 1800s default was therefore left
alone. The stale-lock reclaim path, formerly non-atomic (CAVEAT 2), is now
atomic (lode-ao95; see that half of the tests below) via an mkdir-gated
critical section, and the record's owner token (5th field) that a future
ownership check in `heartbeat` will need is preserved across heartbeat calls
but not yet verified against anything -- see lode-q9pm.

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

   These pins are only as good as the parser under them, which is why one of
   them checks the parser rather than the skill:
   `test_fenced_bash_sees_every_bash_marker_including_indented_ones` asserts
   the shared `tests/conftest.py::bash_fence_blocks` helper sees every bash
   fence in SKILL.md, against an independently-derived count. Without it these
   pins silently covered 20 of 24 blocks (lode-ovgs).
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from conftest import bash_fence_blocks

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
        check=False,
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


def test_heartbeat_preserves_the_existing_owner_token(tmp_path: Path) -> None:
    """MERGE RESOLUTION pin (lode-ao95 x lode-m87j): `heartbeat` must PRESERVE
    field 5 (the owner token) rather than regenerate or blank it -- a
    heartbeat that mints a fresh token every tick would destroy the ownership
    continuity a future check (lode-q9pm) needs to compare against, while
    every other heartbeat test here (which only checks the epoch/timestamp)
    would stay green regardless. This is the exact regression the MERGE NOTE
    in scripts/land-lock.sh's header called out: reverting `heartbeat`'s
    `lock_record "$CUR_TOKEN"` call back to trunk's original, argument-less
    `lock_record` either crashes under `set -u` (the positional is mandatory)
    or, if defaulted instead of reverted, changes the token -- either way
    this test goes red.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    original_token = "deadbeefcafef00d"
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z {original_token}\n")

    result = _run("heartbeat", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = lock.read_text().split()
    assert len(fields) == 5, fields
    assert fields[4] == original_token, (
        "heartbeat changed the owner token -- it must PRESERVE field 5, "
        "never regenerate or blank it (see MERGE RESOLUTION in "
        "scripts/land-lock.sh's header)"
    )
    # The timestamp itself must still have refreshed -- this is not "heartbeat
    # is a no-op" in disguise.
    assert int(fields[2]) > old_epoch


def test_heartbeat_on_a_pre_token_four_field_lock_mints_a_fresh_token(
    tmp_path: Path,
) -> None:
    """Backward compatibility: a lock record predating the owner token
    (lode-aps3-era, four fields) has nothing to preserve, so `heartbeat`
    mints a fresh one -- matching `acquire`'s own shape for the same case --
    rather than crashing or writing an empty 5th field."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run("heartbeat", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = lock.read_text().split()
    assert len(fields) == 5, fields
    assert fields[4], "heartbeat left field 5 empty instead of minting a token"


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
    one second on the 1800s default -- operationally nothing.)"""
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
    by the default threshold -- a genuinely still-running /land pass
    (dispatching several land-review subagents, then a combined re-gate) can
    easily take a few minutes between two Bash invocations."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    two_minutes_ago = int(time.time()) - 120
    lock.write_text(f"12345 host {two_minutes_ago} 2026-01-01T00:00:00Z\n")

    result = _run("acquire", repo=repo)

    assert result.returncode == 1, result.stdout + result.stderr


def test_default_staleness_threshold_is_still_1800s(tmp_path: Path) -> None:
    """Pin the DEFAULT itself, not just that it exceeds two minutes.

    Every other test here passes LAND_LOCK_STALE_SECONDS explicitly, and the
    test above passes for any default over 120s -- so before this pin, the one
    number carrying the whole safety margin could be lowered with nothing going
    red. It is not an arbitrary constant: lode-m87j's `heartbeat` bounds the
    *dangerous* direction (a live pass reclaimed mid-merge), but the window
    must still outlast the longest unheartbeated stretch, and the binding one
    -- a single `land-review` Opus dispatch -- has never been measured. Agent
    dispatches in this repo run to double-digit minutes, so a 600s window is
    the same order as the gap rather than clear of it; the reduction lode-m87j
    proposed was reverted for exactly that reason (see scripts/land-lock.sh,
    CAVEAT 1). Lowering it is lode-cp4o's job, and requires the measurement.

    Asserted behaviourally, from the outside: a lock 1799s old is still held,
    a lock 1801s old is stale. Deliberately not a grep for the literal -- this
    fails if the semantics change, not merely if the digits move.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)

    lock.write_text(f"12345 host {int(time.time()) - 1799} 2026-01-01T00:00:00Z\n")
    just_inside = _run("acquire", repo=repo)
    assert just_inside.returncode == 1, (
        "a lock 1799s old was reclaimed -- LAND_LOCK_STALE_SECONDS was lowered "
        "below 1800s. That is lode-cp4o's decision to make, with measurements "
        "(scripts/land-lock.sh, CAVEAT 1)."
    )

    lock.write_text(f"12345 host {int(time.time()) - 1801} 2026-01-01T00:00:00Z\n")
    just_outside = _run("acquire", repo=repo)
    assert just_outside.returncode == 0, (
        "a lock 1801s old was NOT reclaimed -- LAND_LOCK_STALE_SECONDS was "
        "raised above 1800s, so an abandoned lock now blocks landing for longer "
        f"than the documented 30min: {just_outside.stdout + just_outside.stderr}"
    )


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


def test_land_skill_heartbeats_the_lock_once_per_ticket_in_section_2a() -> None:
    """lode-m87j: the vet loop (Section 2a) heartbeats the lock as the first
    action of every iteration, so the staleness window measures the gap since
    the last ticket's dispatch rather than the whole pass's duration. A dropped
    call site silently reintroduces the acquisition-age exposure this ticket
    exists to close -- pinned the same way acquire and release are above.

    Scope, stated so a reader does not over-trust it: this pins that the call
    EXISTS (and its companion below, that it exists inside an executable
    fence). Neither pins WHERE -- that it is in Section 2a, that it is first in
    the loop body, or that the loop runs once per ticket. Markdown call sites
    have no better mechanical gate available here; placement rests on review.
    """
    text = LAND_SKILL.read_text(encoding="utf-8")

    assert "scripts/land-lock.sh heartbeat" in text, (
        "land/SKILL.md never heartbeats the single-lander lock -- the TTL is "
        "back to measuring acquisition age, not idle time (lode-m87j)"
    )


def _fenced_bash(markdown: str) -> str:
    """The ```bash fences only, concatenated into one string -- what an agent
    actually EXECUTES.

    Scanning the whole file would match the prose that *explains* the old
    defect (it necessarily quotes `trap` and `kill -0`), so the pin has to
    separate what is executed from what is merely described. That split is
    also the point: the fence is the one part of this skill no gate parses,
    which is how the bug survived unnoticed in the first place.

    Thin wrapper over the shared `tests/conftest.py::bash_fence_blocks`
    parser; this function used to carry its own column-0 copy, which silently
    checked 20 of this file's 24 bash blocks (lode-ovgs). Why it was unified
    rather than patched in place, and the parser's known blind spots, live
    next to the parser itself -- deliberately not restated here, per this
    module's own no-second-copy rule above.
    """
    return "\n".join(bash_fence_blocks(markdown))


def test_fenced_bash_sees_every_bash_marker_including_indented_ones() -> None:
    """Regression pin for lode-ovgs, independent of `_fenced_bash`/
    `bash_fence_blocks` itself: derive the EXPECTED fence count a second,
    unrelated way (a plain stripped-line ```bash/```sh marker count) and
    assert the parser's own block count matches it.

    This is the shape the ticket asked for explicitly: a startswith-anchored
    parser undercounts (it saw 20 of 24 blocks against the shipped file when
    this test was written -- 4 indented fences invisible to it), so this
    test would have failed against the pre-fix parser without needing to
    know in advance which blocks are indented or where.

    Sabotage recipe (recorded per this repo's non-vacuous-test standard):
    in tests/conftest.py's `bash_fence_blocks`, replace `stripped = line.strip()`
    with `stripped = line` (i.e. stop stripping before the fence-marker check,
    reintroducing the exact column-0-anchored bug this ticket fixes). Every
    caller of `bash_fence_blocks` -- this test included -- then parses only 20
    blocks against this file instead of 24, and this test goes red reporting
    exactly that mismatch. Restoring `stripped = line.strip()` turns it green
    again. Verified by hand against this exact file while writing this ticket.
    """
    text = LAND_SKILL.read_text(encoding="utf-8")
    expected_marker_count = sum(
        1 for line in text.splitlines() if line.strip() in {"```bash", "```sh"}
    )
    # A sanity floor on the independent count itself -- if this ever drops to
    # 0 the file lost every bash fence, which is a different, louder bug this
    # test should not quietly go vacuous over.
    assert expected_marker_count > 0

    parsed_block_count = len(bash_fence_blocks(text))

    assert parsed_block_count == expected_marker_count, (
        f"parsed {parsed_block_count} ```bash/```sh fenced blocks but "
        f"{expected_marker_count} opening ```bash/```sh markers exist in the "
        "file -- the parser is missing some, e.g. an INDENTED fence a "
        "column-0-anchored scanner cannot see (lode-ovgs)"
    )


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

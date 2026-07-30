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

What this file adds on top of that is the regression gate, in four parts:

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

4. The alive-but-stalled gate-winner displacement (lode-78ih): a gate winner
   that stalls past RECLAIM_GATE_STALE_SECONDS between passing re-validation
   and its destructive `rm`+write used to resume and clobber a later
   reclaimer's fresh record unconditionally. `scripts/land-lock.sh` now
   re-verifies gate ownership immediately before that `rm`; staging the
   displacement deterministically (without a real 30+s sleep) needs a real
   stall somewhere in the middle of one `acquire` invocation, which the
   script's own `LAND_LOCK_TEST_STALL_SECONDS` test-only hook provides --
   never set by any production caller, see the script's own comment at its
   call site.

   The gate's aging is read off `$RECLAIM_GATE`'s own directory mtime (set
   atomically by `mkdir`, lode-78ih), not a separately written epoch file --
   an earlier revision of this same fix used a combined epoch+token record
   with a second writer (a "no creation stamp yet" safety net), and that
   second writer's blind overwrite was OBSERVED, at 32-way contention, to
   erase the real winner's own token, producing a false "lost the race" for
   an UNDISPLACED pass. `test_concurrent_acquire_against_a_stale_lock_has_
   exactly_one_winner` is what caught it -- it was flaky under that design,
   not just failing outright, so treat any reintroduction of a second writer
   to the gate's owner file as a regression even if a single run looks green.

   That stress test deliberately starts every round with NO gate. Extending it
   to start from an already-abandoned one (to exercise the self-heal path) is
   specifically warned against in its own docstring: that arrangement reaches
   residuals land-lock.sh documents as still open, so "exactly one winner"
   stops being a property the design guarantees.
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


def _set_gate_mtime(gate: Path, *, age: int) -> None:
    """Backdate the GATE DIRECTORY's own mtime (lode-78ih) by `age` seconds --
    the aging signal `stat` reads directly, a kernel-managed property `mkdir`
    already sets, never a separately written epoch file (the lode-ao95-era
    design these tests originally pinned)."""
    stamp = time.time() - age
    os.utime(gate, (stamp, stamp))


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

    THE STARTING STATE IS ALSO LOAD-BEARING, in the other direction: each
    round starts with NO gate, so every racer takes the plain ``mkdir`` path
    and exactly one can win. Do NOT extend this test to start rounds with an
    ALREADY-ABANDONED gate in order to exercise the self-heal path -- it looks
    like free extra coverage and it is actually an unsound assertion. On that
    path every racer runs ``rm -rf`` then ``mkdir``, which reaches the two
    residuals land-lock.sh's header documents as open (a racer's ``rm -rf``
    can remove a gate it never judged; and the gateless FRESH path can slip
    into a gate winner's own ``rm``-then-``write_lock`` gap). Both were
    MEASURED live on that arrangement -- 2 of 150 rounds at 32-way under
    28-way CPU saturation, one round with two reclaim winners and one with a
    reclaim plus a fresh winner -- so ``== 1`` is asserting a guarantee the
    design does not currently make, and the arm fails intermittently under
    load rather than gating anything. Closing those residuals is lode-y3dw.
    The self-heal displacement is instead covered DETERMINISTICALLY by
    ``test_stalled_gate_winner_is_displaced_and_aborts_rather_than_clobbering``
    below, which stages the identical interleaving without dice.
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
    be in flight. `mkdir` alone gives the gate a fresh mtime (lode-78ih) --
    no file write is needed to simulate "a live reclaim just started"."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = _write_stale_lock(repo)

    gate = _gate_path(repo)
    gate.mkdir()

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    # Untouched: the gate holder (simulated here) is still the only one
    # allowed to reclaim; the main lock file is exactly as it was.
    assert lock.read_text().split()[2] == str(old_epoch)
    assert gate.exists()


def test_an_empty_gate_is_immediately_ageable_via_its_own_mtime(
    tmp_path: Path,
) -> None:
    """lode-78ih: a reclaimer killed between `mkdir "$LOCK.reclaiming"` and
    writing its owner file leaves a gate with nothing inside it at all. Under
    the lode-ao95-era design (an epoch written INSIDE the gate) that was
    "not yet dated" and needed a second racer to stamp it, or landing could
    wedge permanently once that gate was truly abandoned. Deriving age from
    the GATE DIRECTORY's own mtime instead (lode-78ih) removes that problem
    at the root: `mkdir` sets the directory's mtime atomically, at creation,
    so even a completely empty gate is ageable from the instant it exists --
    no write, no "dating" step, and no separate writer to race against.

    This test pins BOTH directions with the same empty gate: fresh (age 0)
    blocks a concurrent acquire exactly like a populated one would (see
    `test_a_gate_busy_with_a_live_reclaim_is_not_treated_as_abandoned`), and
    backdated past the window it self-heals exactly like a populated one
    would (see `test_an_abandoned_reclaim_gate_is_cleared_and_retried`) --
    demonstrating the owner file's presence or absence never mattered to
    aging in the first place.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = _write_stale_lock(repo)
    gate = _gate_path(repo)

    gate.mkdir()  # nothing written inside -- killed between mkdir and owner
    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert gate.exists(), "a fresh, empty gate must not be treated as abandoned"
    assert lock.read_text().split()[2] == str(old_epoch)

    _set_gate_mtime(gate, age=1000)  # long abandoned, still nothing inside
    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not gate.exists(), "an abandoned empty gate must still be clearable"
    new_fields = lock.read_text().split()
    assert int(new_fields[2]) > old_epoch


def test_an_abandoned_reclaim_gate_is_cleared_and_retried(tmp_path: Path) -> None:
    """The no-wedge half of the fix: a reclaim gate left behind by a
    reclaimer that crashed between winning it and clearing it (age well past
    RECLAIM_GATE_STALE_SECONDS) must NOT block landing forever -- a later
    acquire clears it and retries, successfully reclaiming the still-stale
    main lock. Backdates the GATE DIRECTORY's own mtime (lode-78ih) directly,
    the same signal `stat` reads in the script, rather than an epoch written
    inside it."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = _write_stale_lock(repo)

    gate = _gate_path(repo)
    gate.mkdir()
    (gate / "owner").write_text("some-prior-owner-token\n")
    _set_gate_mtime(gate, age=1000)  # long abandoned

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not gate.exists(), "the abandoned gate must be cleared, not left behind"
    new_fields = lock.read_text().split()
    assert int(new_fields[2]) > old_epoch


# ---------------------------------------------------------------------------
# Gate-ownership re-check (lode-78ih) -- closes the alive-but-stalled-holder
# displacement lode-ao95's header documented but did not fix.
# ---------------------------------------------------------------------------


def test_gate_owner_token_matches_the_acquired_lock_on_an_uncontested_reclaim(
    tmp_path: Path,
) -> None:
    """Sanity check for the mechanism lode-78ih adds: on an ordinary,
    uncontested reclaim (nothing displaces this pass), the new gate-ownership
    check always finds itself still the owner and proceeds -- the fix must
    not turn a normal reclaim into a spurious abort."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    _write_stale_lock(repo)

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "acquired via reclaim" in result.stdout
    lock_token = lock.read_text().split()[4]
    assert lock_token in result.stdout
    # The gate is cleaned up on success -- lode-78ih's bookkeeping
    # ($RECLAIM_GATE/owner) leaves nothing behind.
    assert not _gate_path(repo).exists()


def test_stalled_gate_winner_is_displaced_and_aborts_rather_than_clobbering(
    tmp_path: Path,
) -> None:
    """The exact residual lode-ao95's header documented, reproduced end to
    end and closed by lode-78ih: gate winner A stalls (via the script's own
    LAND_LOCK_TEST_STALL_SECONDS test hook) between passing re-validation and
    its destructive rm+write. While A is stalled, this test backdates A's own
    GATE DIRECTORY's mtime -- standing in for the real 30+s wait
    RECLAIM_GATE_STALE_SECONDS would otherwise require -- so a second,
    UNMODIFIED `acquire` (B) judges A's gate abandoned, clears it, and wins a
    fresh one under its own token, completing a full reclaim. When A resumes,
    it must find the gate no longer shows ITS OWN token as owner (B's own
    successful cleanup has since removed the gate entirely) and abort rather
    than performing its own rm -f "$LOCK" + write on top of B's fresh record.

    Exactly one of A/B may hold the lock afterward -- this is the same
    "exactly one winner" bar as the 32-way stress test above, staged instead
    against the SPECIFIC interleaving that stress test cannot reach (see this
    file's module docstring, part 4, and land-lock.sh's own header for why a
    bare re-validation re-check is not enough)."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    _write_stale_lock(repo)
    gate = _gate_path(repo)

    # Sized from measurement, not habit: A's `owner` file becomes visible in
    # 11-18ms and B's whole `acquire` takes 26-30ms, so the stall has to cover
    # ~45ms (~192ms measured under deliberate 24-way CPU saturation). 2s is a
    # ~10x margin on the worst of those and costs 2s; the original 5s cost 5s
    # for no more coverage. Too SHORT here fails loudly (A wakes early, two
    # winners, the test goes red) rather than silently passing, so this is a
    # flakiness/runtime trade, never a coverage one.
    stall_seconds = 2
    a = subprocess.Popen(
        ["bash", str(SCRIPT), "acquire"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "LAND_LOCK_STALE_SECONDS": "1800",
            "LAND_LOCK_TEST_STALL_SECONDS": str(stall_seconds),
        },
    )
    try:
        # Poll for A to have won the gate and written its own owner file --
        # not a fixed sleep, so this isn't itself a timing gamble. A's stall
        # happens strictly AFTER this write (see land-lock.sh's reclaim
        # loop), so once this is visible A is guaranteed to still be
        # sleeping for (most of) stall_seconds.
        deadline = time.time() + 10
        a_token = ""
        while time.time() < deadline:
            owner_file = gate / "owner"
            if owner_file.exists():
                content = owner_file.read_text().strip()
                if content:
                    a_token = content
                    break
            time.sleep(0.05)
        else:
            a.kill()
            a.communicate(timeout=5)
            raise AssertionError("A never won the gate and wrote its own owner file")

        # Pin the invariant the whole aging scheme rests on, while a real
        # process holds a real gate: `owner` is the ONLY entry inside it. A
        # directory's mtime is bumped by any entry created or removed in it, so
        # a second file written into a gate its holder already owns would
        # refresh the aging clock at every write -- and an abandoned gate whose
        # clock keeps being refreshed can never age out, which is the permanent
        # wedge the gate's staleness bound exists to rule out. Without this
        # assertion that constraint is prose in land-lock.sh's header only, and
        # an edit adding `$RECLAIM_GATE/pid` would pass this whole file.
        assert sorted(p.name for p in gate.iterdir()) == ["owner"], (
            "something other than `owner` is being written inside the reclaim "
            "gate -- see land-lock.sh's gate-aging comment; this silently "
            "refreshes the aging clock and can re-wedge landing"
        )

        # Backdate A's OWN gate directory mtime so B's self-heal judges it
        # abandoned without waiting out the real RECLAIM_GATE_STALE_SECONDS
        # window -- same technique as
        # test_an_abandoned_reclaim_gate_is_cleared_and_retried above,
        # applied here to a gate a REAL concurrent process currently owns
        # rather than a synthetic one. A's own owner file is left
        # untouched -- it still names A's token, exactly as A wrote it;
        # only the DIRECTORY's aging signal is backdated.
        _set_gate_mtime(gate, age=1000)

        b = _run(
            "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
        )
        assert b.returncode == 0, b.stdout + b.stderr
        assert "acquired via reclaim" in b.stdout

        a_stdout, a_stderr = a.communicate(timeout=stall_seconds + 15)
        a_rc = a.returncode
    finally:
        if a.poll() is None:
            a.kill()
            a.communicate(timeout=5)

    winners = [rc for rc in (a_rc, b.returncode) if rc == 0]
    assert len(winners) == 1, (
        f"a: rc={a_rc} out={a_stdout!r} err={a_stderr!r}\n"
        f"b: rc={b.returncode} out={b.stdout!r} err={b.stderr!r}"
    )
    assert a_rc == 1, a_stdout + a_stderr
    assert "lost the race" in a_stderr

    # The final lock must be B's record -- A must never have performed its
    # own rm -f "$LOCK" + write on top of it.
    b_token = lock.read_text().split()[4]
    assert b_token in b.stdout
    assert a_token not in lock.read_text()


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


def test_the_stall_hook_is_set_nowhere_outside_the_tests() -> None:
    """`scripts/land-lock.sh` carries a test-only `LAND_LOCK_TEST_STALL_SECONDS`
    hook that makes it `sleep` while holding the reclaim gate -- i.e. it
    manufactures, on demand, the exact stall that is this lock's documented
    two-winner residual. Its safety rests entirely on nothing in production
    ever setting it, and that is the kind of claim this repo mechanizes rather
    than asserts in a comment.

    Only two files may mention the variable at all: the script that reads it,
    and this test file. In particular a `/land` skill step, a nox session, or an
    `env` block in `.claude/settings*.json` must never set it -- an exported
    value would stall lock acquisition at the one point where stalling is known
    to admit two landers.
    """
    allowed = {
        REPO_ROOT / "scripts" / "land-lock.sh",
        Path(__file__).resolve(),
    }
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.split("\0")

    offenders = []
    for rel in tracked:
        if not rel:
            continue
        path = REPO_ROOT / rel
        if path in allowed or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue  # binary or unreadable: cannot be setting a shell env var
        if "LAND_LOCK_TEST_STALL_SECONDS" in text:
            offenders.append(rel)

    assert not offenders, (
        "land-lock.sh's test-only stall hook is referenced outside "
        f"scripts/land-lock.sh and this test file: {offenders}. If a production "
        "caller ever sets it, /land stalls while holding the reclaim gate -- the "
        "documented two-winner residual, manufactured on purpose."
    )


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

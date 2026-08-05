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
   `git rev-parse --git-common-dir`, to place the lock under the repo's
   shared `.git/` -- repo-global rather than worktree-private, lode-xkpd) --
   no fake git, no mocked subprocess. Reintroducing either half of the old
   design turns these red: a `trap` release makes
   `test_second_acquire_without_release_is_skipped_while_fresh` fail (the
   lock is gone by the next invocation), and a `kill -0` liveness check
   makes `test_fresh_lock_with_an_unreachable_pid_is_not_reclaimed` fail.
   Both mutations were run against this file and confirmed red (5 and 6
   failures of the 14 behavioural tests that existed when that was measured).
   Two tests pin the repo-global lock path (lode-xkpd), and both pin the SAME
   property -- "an acquire from elsewhere in the repo contends on the same
   file" -- from the two directions that can break it:
   `test_lock_path_is_identical_from_a_linked_worktree` (a real `git worktree
   add`; red if `--git-common-dir` reverts to `--git-dir`) and
   `test_lock_path_is_identical_from_a_subdirectory_of_the_main_checkout` (a
   cwd-relative path that stops resolving). Note that neither pins
   `--path-format=absolute` itself on a current git -- see that second test's
   docstring, which is explicit about the limit.

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
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from _gitrepo import _git
from conftest import LAND_SKILL, _fenced_bash, bash_fence_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-lock.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo -- just enough for `git rev-parse --git-common-dir`.

    The `user.email`/`user.name` config is not decoration: any test here that
    goes on to make a COMMIT (the linked-worktree tests below need one, since
    `git worktree add` requires a ref to branch from) fails outright with
    `fatal: empty ident name` on a machine with no ambient global git identity
    -- a fresh clone or a CI container (measured: exit 128). Setting it here
    rather than in those tests matches the seven sibling script-test modules
    that all configure it inside their own `_init_repo`, and keeps this file
    from passing only on machines that happen to have a global identity
    configured.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
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
# A rev-parse failure must land inside the documented 0/1/2 contract, never
# escape as git's own bare 128 (lode-8qkb)
#
# PRE-FIX, `$LOCK="$(git rev-parse ...)/land.lock"` was a bare command
# substitution under `set -euo pipefail`, so running from outside any git
# repository exited with git's raw 128 and a bare `fatal:` -- a status the
# script's own header says it never returns. Each subcommand maps that failure
# onto a DIFFERENT documented exit (the whole reason these are three tests and
# not one), so the script's exit code IS the assertion here; the stderr
# substring pins that the diagnostic is attributed to land-lock rather than
# left as git's unattributable `fatal:`. Why exit 1 and not
# assert-main-checkout.sh's exit 2 lives in the script, next to the code.
#
# `tmp_path` is not itself inside a git repository (unlike a checkout under
# this repo's own tree), so no `GIT_CEILING_DIRECTORIES` dance is needed --
# same fixture shape as tests/test_assert_main_checkout.py's
# test_not_inside_any_repository_is_exit_2_not_a_raw_git_128.
#
# All three sabotage-verified together: reverting the `if ! GIT_COMMON_DIR=...`
# wrap back to the bare form turns all three red with returncode 128.
# ---------------------------------------------------------------------------


def test_acquire_outside_any_git_repository_exits_1_not_a_raw_128(
    tmp_path: Path,
) -> None:
    """`acquire` maps the failure onto its existing exit-1 MACHINE FAULT
    branch -- the same class as "cannot create the lockfile", so a caller is
    never told "another lander is running" when the machine is at fault."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("acquire", repo=outside)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "land-lock: MACHINE FAULT" in result.stderr
    assert "skipping this tick" in result.stderr.lower()


def test_heartbeat_outside_any_git_repository_exits_1_not_a_raw_128(
    tmp_path: Path,
) -> None:
    """`heartbeat` has its own documented exit-1 "could not write the lock
    file" branch, with wording distinct from acquire's MACHINE FAULT one --
    heartbeat is bookkeeping, so its diagnostic must not read as a landing
    verdict."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("heartbeat", repo=outside)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "land-lock: heartbeat" in result.stderr


def test_release_outside_any_git_repository_still_exits_0(tmp_path: Path) -> None:
    """`release` is documented to ALWAYS exit 0 ("rm -f is idempotent"), and
    that promise must survive this fix -- so this is the one subcommand whose
    documented exit is unchanged by the wrap, yet it still went 128 pre-fix.
    It must reach 0 through the new branch, with a diagnostic, rather than
    silently."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("release", repo=outside)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "land-lock: release" in result.stderr


# ---------------------------------------------------------------------------
# Repo-global lock path (lode-xkpd) -- `--git-dir` is worktree-PRIVATE
# ---------------------------------------------------------------------------


def test_lock_path_is_identical_from_a_linked_worktree(tmp_path: Path) -> None:
    """Why a worktree-private gitdir would break the lock is explained at the
    `LOCK=` line in scripts/land-lock.sh, not restated here (this module's
    no-second-copy policy, above).

    What this test contributes: it is red against the `--git-dir` form -- the
    worktree's `acquire` succeeds instead of being skipped -- and green against
    `--git-common-dir`. Verified by running exactly that mutation (lode-xkpd);
    both acquires then reported success with different tokens."""
    repo = _init_repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feat", "trunk")

    from_main = _run("acquire", repo=repo)
    assert from_main.returncode == 0, from_main.stdout + from_main.stderr
    assert _lock_path(repo).exists()

    from_worktree = _run("acquire", repo=worktree)

    # If the lock path were worktree-relative, this `acquire` would succeed
    # (it would be writing to a DIFFERENT, not-yet-existing file) instead of
    # being skipped as "still held".
    assert from_worktree.returncode == 1, (
        "acquire from the linked worktree succeeded even though the main "
        "checkout already holds the lock -- the lock path is not repo-global"
        f"\nmain: {from_main.stdout}{from_main.stderr}"
        f"\nworktree: {from_worktree.stdout}{from_worktree.stderr}"
    )
    assert "skipping this tick" in from_worktree.stderr

    # And no SEPARATE, worktree-private lock file was created either -- the
    # worktree's `acquire` genuinely contended on the SAME file, not merely
    # failed for some unrelated reason while writing its own private one.
    #
    # ASK GIT where the private gitdir is rather than hand-spelling it: the
    # directory under `.git/worktrees/` is named after the worktree's DIRECTORY
    # basename (`wt`), NOT its branch (`feat`), so a hand-spelled
    # `.git/worktrees/feat/...` names a path git never creates under EITHER
    # form of the fix -- unconditionally absent, and therefore proof of
    # nothing. Same reasoning as `_gate_path` above, and the same repo-wide
    # fiat (docs/conventions.md, "Derive identifiers, never retype them").
    private_gitdir = Path(
        _git(
            worktree, "rev-parse", "--path-format=absolute", "--git-dir"
        ).stdout.strip()
    )
    private_lock = private_gitdir / "land.lock"
    assert not private_lock.exists(), (
        "a worktree-private lock file exists -- the worktree computed a "
        f"DIFFERENT $LOCK ({private_lock}) than the main checkout"
    )


def test_lock_path_is_identical_from_a_subdirectory_of_the_main_checkout(
    tmp_path: Path,
) -> None:
    """A hazard the linked-worktree test above cannot catch, because it is not
    about worktrees at all -- see the `--path-format=absolute` paragraph at the
    `LOCK=` line in scripts/land-lock.sh for why cwd-relativity matters.

    Be precise about what this does and does not pin, because it is weaker than
    it looks. Dropping `--path-format=absolute` alone does NOT turn it red on a
    git that resolves the relative answer against cwd (measured: git 2.43
    answers `../../.git`, which still names the right file). The contract it
    actually guards is "an acquire from a subdirectory contends on the SAME
    file as one from the root", which is the property mutual exclusion needs.
    It DOES go red once the path stops resolving from cwd -- the older-git
    behaviour of returning a bare `.git` relative to the TOPLEVEL. Verified
    non-vacuous by mutating $LOCK to a hardcoded `.git/land.lock`: the subdir
    acquire then takes the MACHINE FAULT branch, so `returncode == 1` passes
    coincidentally and the STDERR assertion below is the one that catches it.
    Keep both."""
    repo = _init_repo(tmp_path)
    subdir = repo / "sub" / "deeper"
    subdir.mkdir(parents=True)

    from_root = _run("acquire", repo=repo)
    assert from_root.returncode == 0, from_root.stdout + from_root.stderr
    assert _lock_path(repo).exists()

    from_subdir = _run("acquire", repo=subdir)

    assert from_subdir.returncode == 1, (
        "acquire from a subdirectory of the main checkout succeeded even "
        "though the root already holds the lock -- the lock path is cwd-relative"
        f"\nroot: {from_root.stdout}{from_root.stderr}"
        f"\nsubdir: {from_subdir.stdout}{from_subdir.stderr}"
    )
    assert "skipping this tick" in from_subdir.stderr

    # No second lock file anywhere under the subdirectory -- the subdir's
    # `acquire` contended on the SAME file rather than writing its own.
    assert not list(subdir.rglob("land.lock")), (
        "a lock file was created under the subdirectory -- $LOCK resolved "
        "relative to cwd instead of to the shared .git"
    )


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


def test_default_staleness_threshold_is_still_about_1800s(tmp_path: Path) -> None:
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

    Asserted behaviourally, from the outside: a lock 1700s old is still held,
    a lock 1900s old is stale. Deliberately not a grep for the literal -- this
    fails if the semantics change, not merely if the digits move.

    The 100s margins are deliberate, and were 1s until lode-44cq: the age is
    stamped from ONE clock read here, in Python, then measured against a
    SECOND, later read inside `land-lock.sh`'s own subprocess, so under load
    (`-n 8`, interpreter/bash startup, git work) that gap could exceed a
    second and age a 1799s lock past 1800s -- a false, one-sided red. This is
    the same hazard `test_fresh_lock_is_not_reclaimed_under_a_large_threshold`
    calls out for its own boundary; the margin is how this file has always
    handled it (every other threshold test here uses a 2x-or-wider ratio).

    The trade is real: the pin now catches only a default outside
    (1700s, 1900s), so a lowering to e.g. 1750s would slip through. Measured
    on this branch -- defaults of 900s and 3600s each turn it red, 1750s does
    not.

    Rejected as worse: injecting a pinned "now" into the script. That would
    put a test-only env var directly into the *decision* input of a
    production lock's staleness path, unlike the existing
    LAND_LOCK_TEST_STALL_SECONDS hook, which can only sleep and cannot change
    a decision.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)

    lock.write_text(f"12345 host {int(time.time()) - 1700} 2026-01-01T00:00:00Z\n")
    well_inside = _run("acquire", repo=repo)
    assert well_inside.returncode == 1, (
        "a lock 1700s old was reclaimed -- LAND_LOCK_STALE_SECONDS was lowered "
        "well below 1800s. That is lode-cp4o's decision to make, with "
        "measurements (scripts/land-lock.sh, CAVEAT 1)."
    )

    lock.write_text(f"12345 host {int(time.time()) - 1900} 2026-01-01T00:00:00Z\n")
    well_outside = _run("acquire", repo=repo)
    assert well_outside.returncode == 0, (
        "a lock 1900s old was NOT reclaimed -- LAND_LOCK_STALE_SECONDS was "
        "raised well above 1800s, so an abandoned lock now blocks landing for "
        f"much longer than the documented 30min: "
        f"{well_outside.stdout + well_outside.stderr}"
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


_STALL_HOOK_VAR = "LAND_LOCK_TEST_STALL_SECONDS"

# .beads/*.jsonl is beads' passive export -- regenerated and re-staged by the
# pre-commit hook on EVERY commit (import.auto: false, lode-6ra), so it is
# never itself a production caller of anything. A bd issue body that happens
# to name `_STALL_HOOK_VAR` (this ticket's own description does) would
# otherwise redden this scan for a reason unrelated to any real caller --
# excluded for that reason, not to widen what the scan is willing to miss.
_STALL_HOOK_SCAN_EXCLUDED_RELPATHS = {
    ".beads/issues.jsonl",
    ".beads/interactions.jsonl",
}


def _stall_hook_offenders(repo_root: Path, *, allowed: set[Path]) -> list[str]:
    """Every git-TRACKED file under `repo_root` that mentions `_STALL_HOOK_VAR`,
    other than `allowed` and the passive bd export. Tracked-only by
    construction (`git ls-files`): see the caller's docstring for what that
    does and does not promise.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.split("\0")

    offenders = []
    for rel in tracked:
        if not rel or rel in _STALL_HOOK_SCAN_EXCLUDED_RELPATHS:
            continue
        path = repo_root / rel
        if path in allowed or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue  # binary or unreadable: cannot be setting a shell env var
        if _STALL_HOOK_VAR in text:
            offenders.append(rel)
    return offenders


def test_the_stall_hook_is_set_nowhere_outside_the_tests() -> None:
    """`scripts/land-lock.sh` carries a test-only `LAND_LOCK_TEST_STALL_SECONDS`
    hook that makes it `sleep` while holding the reclaim gate -- i.e. it
    manufactures, on demand, the exact stall that is this lock's documented
    two-winner residual. Its safety rests entirely on nothing in production
    ever setting it, and that is the kind of claim this repo mechanizes rather
    than asserts in a comment.

    Two TRACKED files may mention the variable -- the script that reads it and
    this test file -- plus the two excluded exports named at the bottom of this
    docstring. In particular a `/land` skill step, a nox session, or an `env`
    block in the TRACKED `.claude/settings.json` must never set it: an exported
    value would stall lock acquisition at the one point where stalling is known
    to admit two landers.

    This scan is `git ls-files` (tracked files only), so it CANNOT see
    `.claude/settings.local.json` -- the one file CLAUDE.md's "New machine
    setup" designates as the machine-local `env` home, and gitignored
    precisely so it stays untracked (.gitignore:4). That is the single most
    likely place a developer would actually export the variable, and it is
    outside this test's reach by construction; this test makes no promise
    about it.

    `.beads/issues.jsonl` and `.beads/interactions.jsonl` ARE tracked but are
    excluded anyway -- see `_STALL_HOOK_SCAN_EXCLUDED_RELPATHS` above.
    """
    allowed = {
        REPO_ROOT / "scripts" / "land-lock.sh",
        Path(__file__).resolve(),
    }

    offenders = _stall_hook_offenders(REPO_ROOT, allowed=allowed)

    assert not offenders, (
        "land-lock.sh's test-only stall hook is referenced outside "
        f"scripts/land-lock.sh and this test file: {offenders}. If a production "
        "caller ever sets it, /land stalls while holding the reclaim gate -- the "
        "documented two-winner residual, manufactured on purpose."
    )


def test_the_stall_hook_scan_exclusion_is_not_vacuous(tmp_path: Path) -> None:
    """The `.beads/*.jsonl` exclusion above must actually DO something, and
    must not swallow an ordinary tracked file on its way past the export.
    Stage a throwaway repo with the variable planted in BOTH
    `.beads/issues.jsonl` and an unrelated tracked file, and confirm the scan
    stays green for the former while still reporting the latter.

    What this pins, stated at the strength it was MEASURED at (each mutation
    applied to `_stall_hook_offenders` and re-run): emptying
    `_STALL_HOOK_SCAN_EXCLUDED_RELPATHS`, or widening the skip to swallow
    every path or every `*.py`, each turn this test RED. What it does NOT pin
    is the exclusion's exact shape -- broadening the skip to a `.beads/`
    prefix, to any `*.jsonl`, or simply adding another relpath to the set all
    still PASS, since these two planted files are the only ones here. Read it
    as "the exclusion is live and not all-consuming", not as proof that it can
    never be widened into a loophole.
    """
    repo = _init_repo(tmp_path)
    beads_dir = repo / ".beads"
    beads_dir.mkdir()
    (beads_dir / "issues.jsonl").write_text(
        f'{{"id": "fake", "description": "{_STALL_HOOK_VAR}"}}\n'
    )
    offender = repo / "some_module.py"
    offender.write_text(f"{_STALL_HOOK_VAR} = 1\n")
    _git(repo, "add", "-A")

    # The export fixture proves nothing unless git genuinely TRACKS it: a
    # machine-global `core.excludesFile` matching `*.jsonl` would drop it from
    # `git ls-files` and leave this test green with the exclusion doing
    # nothing at all -- the same "passes only on machines configured like
    # mine" trap `_init_repo`'s user.email/user.name comment exists to avoid.
    assert ".beads/issues.jsonl" in _git(repo, "ls-files").stdout.split()

    offenders = _stall_hook_offenders(repo, allowed=set())

    assert offenders == ["some_module.py"], offenders


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

    The independent count must recognize the same FENCE SHAPES the parser
    does, or it stops being an independent second derivation and becomes a
    stale narrower one: lode-p4qb widened `bash_fence_blocks` to open on
    four-or-more backticks and on `~~~`, and against an exactly-three-backtick
    marker count the first author to write the four-backtick form -- the one
    the parser's own docstring says an author MUST use for a block containing
    a ```-prefixed line -- would fail this test on a CORRECT parse, with a
    message blaming the parser for missing a fence it actually found.
    Measured while widening it: one added four-backtick (or tilde) bash fence
    took land/SKILL.md to 25 parsed against 24 markers. What stays independent
    is the METHOD -- a flat per-line marker count, no state machine, no call
    into the parser -- not the grammar.

    KNOWN GAP on one axis, opened by lode-kjei and left deliberately unfixed:
    the parser now refuses to open a bash fence NESTED inside an enclosing
    non-bash fence (CommonMark -- a fence cannot open inside an open fence), but
    a flat per-line count cannot see nesting and still counts the inner marker.
    Measured on this exact file while reviewing lode-kjei: as shipped, 24
    markers / 24 parsed, green; append one illustrative ````text block
    containing a ```bash example and it becomes 25 markers / 24 parsed, so this
    test goes RED blaming the parser for missing a fence it correctly declined.
    Latent -- zero nested fence openers exist across the repo's 58 tracked .md
    files. Not fixed here because the only fix is to make the count
    nesting-aware, i.e. give it a state machine, which is precisely the
    independence this test trades on. If someone ever adds such a block to
    land/SKILL.md, the answer is to re-derive the expected count some third way,
    NOT to relax the parser.
    """
    text = LAND_SKILL.read_text(encoding="utf-8")
    opening_marker = re.compile(r"^(?:`{3,}|~{3,})\s*(?:bash|sh)$")
    expected_marker_count = sum(
        1 for line in text.splitlines() if opening_marker.match(line.strip())
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

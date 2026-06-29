"""Tests for lode.lock — single-instance advisory lock (lode-i05.2).

Acceptance criteria (bd show lode-i05.2):
  - A second acquisition refuses while the lock is held by a live pid and names
    that pid.
  - The lock releases on clean exit.
  - A lock left by a no-longer-running pid is reclaimed, not treated as held.
"""

import os
import subprocess
from pathlib import Path

import pytest

from lode.lock import LockHeld, WorkerLock, lock_path


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Fake DB path; lock file is derived from it."""
    return tmp_path / "lode.db"


# ---------------------------------------------------------------------------
# Lock-path derivation
# ---------------------------------------------------------------------------


def test_lock_path_is_beside_db(db_path: Path) -> None:
    assert lock_path(db_path) == db_path.parent / "lode.db.lock"


# ---------------------------------------------------------------------------
# Acquire / release basics
# ---------------------------------------------------------------------------


def test_acquire_creates_lockfile_with_current_pid(db_path: Path) -> None:
    lk = WorkerLock(db_path)
    lk.acquire()
    try:
        lf = lock_path(db_path)
        assert lf.exists()
        assert lf.read_text().strip() == str(os.getpid())
    finally:
        lk.release()


def test_release_removes_lockfile(db_path: Path) -> None:
    lk = WorkerLock(db_path)
    lk.acquire()
    lk.release()
    assert not lock_path(db_path).exists()


def test_release_is_idempotent(db_path: Path) -> None:
    """Double-release must not raise."""
    lk = WorkerLock(db_path)
    lk.acquire()
    lk.release()
    lk.release()  # should be a no-op


def test_acquire_creates_parent_dirs(tmp_path: Path) -> None:
    """acquire() must mkdir $LODE_HOME if it doesn't exist yet."""
    db = tmp_path / "nonexistent" / "subdir" / "lode.db"
    lk = WorkerLock(db)
    lk.acquire()
    try:
        assert lock_path(db).exists()
    finally:
        lk.release()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_acquires_and_releases(db_path: Path) -> None:
    lf = lock_path(db_path)
    with WorkerLock(db_path):
        assert lf.exists()
    assert not lf.exists()


def test_context_manager_releases_on_body_exception(db_path: Path) -> None:
    """Lock is released even when the body raises."""
    lf = lock_path(db_path)
    with pytest.raises(RuntimeError):
        with WorkerLock(db_path):
            assert lf.exists()
            raise RuntimeError("inner failure")
    assert not lf.exists()


# ---------------------------------------------------------------------------
# Live PID → refuse (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_live_pid_refuses_second_acquire(db_path: Path) -> None:
    """A lockfile written by our own PID is live; a second acquire must refuse."""
    lf = lock_path(db_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(os.getpid()))

    with pytest.raises(LockHeld):
        WorkerLock(db_path).acquire()


def test_refuse_message_names_the_pid(db_path: Path) -> None:
    """The LockHeld message must include the holding pid."""
    lf = lock_path(db_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(os.getpid()))

    with pytest.raises(LockHeld, match=rf"pid {os.getpid()}"):
        WorkerLock(db_path).acquire()


# ---------------------------------------------------------------------------
# Dead PID → reclaim (acceptance criterion 3)
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """Return a PID guaranteed to be no longer running.

    Spawns a trivial subprocess, captures its PID, waits for it to exit, then
    returns the now-dead PID.  Using Popen avoids the multi-threaded-fork
    deprecation warning that ``os.fork()`` triggers inside pytest.
    """
    proc = subprocess.Popen(["true"])  # noqa: S603, S607
    pid = proc.pid
    proc.wait()
    return pid


def test_stale_lock_is_reclaimed(db_path: Path) -> None:
    """A lockfile whose PID is dead must be overwritten with our PID."""
    dead_pid = _dead_pid()
    lf = lock_path(db_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(dead_pid))

    lk = WorkerLock(db_path)
    lk.acquire()
    try:
        assert lf.read_text().strip() == str(os.getpid())
    finally:
        lk.release()


def test_stale_lock_does_not_raise(db_path: Path) -> None:
    """Stale lock must not raise LockHeld."""
    dead_pid = _dead_pid()
    lf = lock_path(db_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(dead_pid))

    lk = WorkerLock(db_path)
    lk.acquire()
    lk.release()


# ---------------------------------------------------------------------------
# Corrupt lockfile → treat as stale
# ---------------------------------------------------------------------------


def test_corrupt_lockfile_is_treated_as_stale(db_path: Path) -> None:
    """Unreadable / non-integer content must not crash — treat as stale."""
    lf = lock_path(db_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("not-a-pid")

    lk = WorkerLock(db_path)
    lk.acquire()
    try:
        assert lf.read_text().strip() == str(os.getpid())
    finally:
        lk.release()

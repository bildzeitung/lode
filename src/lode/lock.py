"""Single-instance advisory lock for the lode async worker (lode-i05.2).

Lockfile + PID beside the DB so at most one worker runs at a time.
This is NOT a data-integrity guard — CAS + SQLite serialization own correctness
(docs/storage.md:95-104).  It exists solely so two workers don't run duplicate,
racing enrichment/embedding loops and double-spend Claude Batches.

Usage::

    from lode.config import default_db_path
    from lode.lock import WorkerLock

    with WorkerLock(default_db_path()):
        run_worker()   # at most one owner; clean exit releases the lock
"""

import os
from pathlib import Path
from types import TracebackType


class LockHeld(RuntimeError):
    """Raised when a live process already holds the advisory lock."""


def lock_path(db_path: Path) -> Path:
    """Return the advisory lockfile path: ``<db_path>.lock`` beside the DB.

    ``default_db_path()`` → ``$LODE_HOME/lode.db`` gives
    ``$LODE_HOME/lode.db.lock`` (docs/configuration.md, docs/storage.md:100).
    """
    return Path(str(db_path) + ".lock")


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* names a running process on this machine.

    Uses ``os.kill(pid, 0)`` — the POSIX best-effort liveness probe.
    ``PermissionError`` means the process exists but we can't signal it (alive).
    ``ProcessLookupError`` means the process is gone (dead / stale).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, no signal permission
    return True


class WorkerLock:
    """Advisory lockfile context manager for the lode async worker.

    **Acquire behaviour** (``__enter__`` / ``acquire``):

    - File absent → create it, write current PID, proceed.
    - File present, PID alive → raise :exc:`LockHeld` naming the holding PID.
    - File present, PID dead (stale) → overwrite with current PID and proceed.
    - File present, corrupt content → treat as stale, overwrite.

    **Release behaviour** (``__exit__`` / ``release``): remove the lockfile.
    The ``finally`` semantics of the context-manager protocol guarantee removal
    even when the body raises, so a normal exit always frees the lock immediately.

    Args:
        db_path: Path to the SQLite DB (e.g. ``default_db_path()``).  The lock
            file is placed beside it as ``<db_path>.lock``.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock = lock_path(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Acquire the lock or raise :exc:`LockHeld`.

        Can be called directly instead of using the context manager when the
        caller manages lifecycle explicitly; ``release()`` must then be called.
        """
        if self._lock.exists():
            pid = self._read_pid()
            if pid is not None and _pid_alive(pid):
                raise LockHeld(f"another lode worker is running (pid {pid})")
            # Stale or corrupt — fall through to reclaim.

        # Create parent dirs if $LODE_HOME doesn't exist yet.
        self._lock.parent.mkdir(parents=True, exist_ok=True)
        self._lock.write_text(str(os.getpid()))

    def release(self) -> None:
        """Remove the lockfile.  Safe to call even if it no longer exists."""
        try:
            self._lock.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "WorkerLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_pid(self) -> int | None:
        """Read PID from lockfile; return None on unreadable / non-integer content."""
        try:
            return int(self._lock.read_text().strip())
        except ValueError, OSError:
            return None

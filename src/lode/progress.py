"""Progress/heartbeat instrumentation for long-running ``lode work`` operations
(lode-olmi.15).

``lode work`` (one-shot, ``--loop``, and ``--wait`` alike) makes several
potentially slow, blocking calls per pass -- :func:`lode.reconcile.reconcile`'s
step registry, :func:`lode.worker.drain`'s two Batches-API pre-steps, the main
claim/run loop (an ``embed`` job can trigger a first-use fastembed/ONNX model
load), and the Anthropic Batches API network calls themselves -- with **zero**
output while any of them actually runs: the existing ``typer.echo``/``log``
lines in ``cli.work`` and ``drain`` only fire *after* a step returns. A
one-shot run blocked inside any of these produces no visible sign of what is
happening until it either finishes or hangs forever (lode-olmi.12's
hypothesis).

:func:`op_progress` closes that gap: it logs an immediate ``"{name}:
starting"`` line naming the operation, a periodic ``"{name}: still running
({elapsed}s)"`` heartbeat (a daemon thread, so it never blocks or delays the
wrapped work) if the operation outlives ``heartbeat_interval_s``, and a final
``"{name}: done"``/``"{name}: failed"`` line with the elapsed duration on
exit. This uses the stdlib ``logging`` module rather than ``typer.echo`` --
lode's existing convention (``src/lode/logconfig.py``) already mirrors
root-logger output to stderr at ``INFO`` by default for plain CLI commands,
so these lines are visible on a plain ``lode work`` run without threading
``typer`` through non-CLI layers (``worker.py``, ``reconcile.py``,
``embedding.py``). This also matches the acceptance criterion's own "progress
/log line" wording.

A heartbeat alone does not literally bound an operation's duration -- for
pure local computation (a SQL scan, an in-process ONNX load) there is no safe
way to abort mid-flight without cooperation from the callee, so making the
wait *visible* (never silent) is the fix there. Network calls that *can* be
given a real client-side timeout still get one directly at the call site
(e.g. the Anthropic Batches API calls in ``enrich.py``, via
``Settings.anthropic_call_timeout_s``) -- ``op_progress`` and an explicit
timeout are complementary, not alternatives.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)

#: Default heartbeat cadence when a caller doesn't have (or want to thread
#: through) a resolved ``Settings.progress_heartbeat_interval_s``.
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0


@contextmanager
def op_progress(
    name: str, *, heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
) -> Iterator[None]:
    """Log start/heartbeat/end lines (at INFO) for a named long-running operation.

    Emits ``"{name}: starting"`` immediately on entry. While the wrapped block
    is running, a daemon thread emits ``"{name}: still running ({elapsed}s)"``
    every ``heartbeat_interval_s`` seconds -- so a genuinely stuck op is never
    silent, even though this alone doesn't abort it (see module docstring).
    On a clean exit, logs ``"{name}: done ({elapsed}s)"``; on an exception,
    logs ``"{name}: failed after {elapsed}s ({exc})"`` and re-raises
    unchanged -- this never swallows or alters the wrapped block's outcome.
    """
    start = time.monotonic()
    log.info("%s: starting", name)
    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(heartbeat_interval_s):
            log.info("%s: still running (%.0fs)", name, time.monotonic() - start)

    thread = threading.Thread(target=_heartbeat, name=f"progress-{name}", daemon=True)
    thread.start()
    try:
        yield
    except BaseException as exc:
        # Format the terminal line here (while `exc` is bound) but emit it in
        # `finally`, so the thread-teardown (stop + join) lives in one place
        # for both the success and failure paths.
        outcome = f"failed after {time.monotonic() - start:.1f}s ({exc})"
        raise
    else:
        outcome = f"done ({time.monotonic() - start:.1f}s)"
    finally:
        stop.set()
        thread.join(timeout=1.0)
        log.info("%s: %s", name, outcome)

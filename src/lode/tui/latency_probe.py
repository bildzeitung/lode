"""Lightweight, toggleable event-loop-lag instrumentation (lode-0wj.2 lag-diagnosis spike).

The capture screen's passive related-notes pass (:func:`lode.tui.related.find_related_notes`)
runs off the UI thread via ``asyncio.to_thread`` (:mod:`lode.tui.screens.capture`), which
only actually relieves typing if the offloaded call releases the GIL while it runs. This
module gives that claim an observable signal in the real app: an asyncio "heartbeat" that
sleeps a fixed short interval on the **same event loop** Textual's key handling and repaint
run on, and logs how much *longer* each sleep actually took than requested. A free loop's
sleeps return on time (near-zero lag); a loop starved by a GIL-holding background call comes
back late by roughly the same amount a keystroke on that loop would also be delayed by --
this is the keystroke->render latency proxy this spike uses (see
``tests/test_capture_lag_diagnosis.py`` for the offline reproduction against the seeded
corpus and the pass/fail verdict).

Entirely opt-in and zero-cost by default: :func:`probe_event_loop_lag` is only ever started
by :mod:`lode.tui.screens.capture` when its logger is at DEBUG
(``logging.getLogger(__name__).isEnabledFor(logging.DEBUG)``), i.e. ``LODE_LOG_LEVEL=DEBUG``
(:mod:`lode.logconfig`) -- no new ``Settings`` knob, reusing the toggle every other lode
module already logs behind (this is a diagnostic instrument, not a tunable).
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

#: How often the heartbeat samples loop lag. Short enough to catch a stall within a
#: keystroke's felt latency, long enough to not be a meaningful load of its own.
HEARTBEAT_INTERVAL_S = 0.05

#: Log a tick's lag at WARNING (rather than DEBUG) once it is at least this far past
#: the interval -- big enough to be a felt keystroke stall, not scheduler jitter.
_WARN_THRESHOLD_MS = 50.0


async def probe_event_loop_lag(interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
    """Run forever, logging how late each ``interval_s`` sleep comes back.

    Cancel the task (e.g. via Textual's worker cancellation on screen unmount) to
    stop it -- it never exits on its own. Callers should gate *starting* it on
    ``log.isEnabledFor(logging.DEBUG)`` (see the module docstring) so it costs
    nothing when instrumentation is off.
    """
    while True:
        start = time.monotonic()
        await asyncio.sleep(interval_s)
        lag_ms = (time.monotonic() - start - interval_s) * 1000
        if lag_ms >= _WARN_THRESHOLD_MS:
            log.warning(
                "event-loop lag: %.1fms over a %.0fms tick (loop was starved)",
                lag_ms,
                interval_s * 1000,
            )
        else:
            log.debug(
                "event-loop lag: %.1fms over a %.0fms tick", lag_ms, interval_s * 1000
            )

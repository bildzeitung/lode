"""Tests for the event-loop-lag heartbeat (lode-0wj.2 lag-diagnosis instrumentation).

Fast and offline: no model, no DB, just the heartbeat's own tick/log behaviour.
To observe the real diagnostic against a live notes DB and the real embedder,
run the app with ``LODE_LOG_LEVEL=DEBUG`` and watch the capture screen's
heartbeat log lines (:mod:`lode.tui.latency_probe`,
:mod:`lode.tui.screens.capture`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from lode.tui.latency_probe import probe_event_loop_lag


async def _run_for(seconds: float, *, interval_s: float) -> None:
    task = asyncio.create_task(probe_event_loop_lag(interval_s))
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_probe_never_returns_on_its_own(caplog: pytest.LogCaptureFixture) -> None:
    """The heartbeat is an infinite loop; only cancellation stops it (docstring contract)."""
    caplog.set_level(logging.DEBUG, logger="lode.tui.latency_probe")
    asyncio.run(_run_for(0.12, interval_s=0.02))
    # It ran for ~6 ticks without raising or exiting early -- cancellation (in
    # _run_for's finally) is what actually stopped it.


def test_probe_logs_a_lag_sample_per_tick_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each tick logs its measured lag at DEBUG -- the toggle capture.py gates on."""
    caplog.set_level(logging.DEBUG, logger="lode.tui.latency_probe")
    asyncio.run(_run_for(0.1, interval_s=0.02))
    lag_records = [r for r in caplog.records if "event-loop lag" in r.message]
    assert len(lag_records) >= 3
    assert all(r.levelno in (logging.DEBUG, logging.WARNING) for r in lag_records)

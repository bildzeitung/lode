"""Tests for lode.progress.op_progress (lode-olmi.15).

op_progress is the shared start/heartbeat/end logging primitive threaded
through reconcile.py, worker.py, and embedding.py to make a long-running
`lode work` operation always visible instead of silent. These tests exercise
the primitive directly, independent of any of its call sites.
"""

import logging
import time

import pytest

from lode.progress import op_progress


def test_op_progress_logs_starting_and_done(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO), op_progress("my_op"):
        pass

    assert "my_op: starting" in caplog.text
    assert "my_op: done" in caplog.text
    assert "still running" not in caplog.text


def test_op_progress_yields_control_to_the_wrapped_block() -> None:
    """The context manager doesn't swallow or alter the wrapped block's result."""
    ran = False
    with op_progress("my_op"):
        ran = True
    assert ran is True


def test_op_progress_heartbeats_a_slow_block(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        with op_progress("slow_op", heartbeat_interval_s=0.05):
            time.sleep(0.25)

    assert "slow_op: starting" in caplog.text
    assert "slow_op: still running" in caplog.text
    assert "slow_op: done" in caplog.text


def test_op_progress_does_not_heartbeat_a_fast_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A block that finishes well within the interval gets no heartbeat line."""
    with caplog.at_level(logging.INFO):
        with op_progress("fast_op", heartbeat_interval_s=10.0):
            pass

    assert "fast_op: starting" in caplog.text
    assert "fast_op: done" in caplog.text
    assert "still running" not in caplog.text


def test_op_progress_logs_failed_and_reraises_on_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO), pytest.raises(ValueError, match="boom"):
        with op_progress("failing_op"):
            raise ValueError("boom")

    assert "failing_op: starting" in caplog.text
    assert "failing_op: failed" in caplog.text
    assert "boom" in caplog.text
    # Never logs a spurious "done" alongside "failed".
    assert "failing_op: done" not in caplog.text


def test_op_progress_heartbeat_thread_stops_after_block_exits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No heartbeat line appears once the block (and its context) has exited.

    Regression guard: op_progress joins its heartbeat thread before
    returning, so a heartbeat firing after the block completes (a leaked
    background thread) would be a bug this test would catch.
    """
    with caplog.at_level(logging.INFO):
        with op_progress("op", heartbeat_interval_s=0.05):
            pass
        caplog.clear()
        time.sleep(0.2)

    assert caplog.text == ""

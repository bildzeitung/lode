"""Tests for lode.logconfig — application logging setup (lode-txh.4).

Asserts the acceptance criterion that logging is configured: the level resolves
from an explicit arg, then ``LODE_LOG_LEVEL``, then a sensible default, and
configuring sets the root logger level.
"""

import logging
from pathlib import Path

import pytest

from lode.logconfig import LOG_LEVEL_ENV, configure_logging, resolve_level


def _lode_file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, "_lode_file", False)]


def test_default_level_is_info(monkeypatch) -> None:
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    assert resolve_level() == logging.INFO


def test_env_var_sets_level(monkeypatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "debug")  # case-insensitive
    assert resolve_level() == logging.DEBUG


def test_explicit_arg_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
    assert resolve_level("WARNING") == logging.WARNING


def test_int_level_passes_through() -> None:
    assert resolve_level(logging.ERROR) == logging.ERROR


def test_unknown_level_raises() -> None:
    with pytest.raises(ValueError):
        resolve_level("LOUD")


def test_configure_sets_root_level(monkeypatch) -> None:
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    returned = configure_logging("WARNING")
    assert returned == logging.WARNING
    assert logging.getLogger().level == logging.WARNING


def test_log_dir_creates_dir_and_writes_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    try:
        configure_logging("INFO", log_dir=log_dir)
        assert log_dir.is_dir()
        logging.getLogger("lode.test").info("hello")
        assert (
            (log_dir / "lode.log").read_text(encoding="utf-8").strip().endswith("hello")
        )
    finally:
        for handler in _lode_file_handlers():
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_file_handler_does_not_accumulate(tmp_path: Path) -> None:
    # Repeated configuration re-points the single lode file handler rather than
    # stacking one open file per call (the group callback runs per command).
    try:
        configure_logging("INFO", log_dir=tmp_path / "a")
        configure_logging("INFO", log_dir=tmp_path / "b")
        handlers = _lode_file_handlers()
        assert len(handlers) == 1
        assert (tmp_path / "b" / "lode.log") == Path(handlers[0].baseFilename)
    finally:
        for handler in _lode_file_handlers():
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_no_file_handler_without_log_dir() -> None:
    # The stderr-only default (no log_dir) attaches no file handler, so library
    # callers and the level-only tests never create files.
    before = set(logging.getLogger().handlers)
    configure_logging("INFO")
    new = set(logging.getLogger().handlers) - before
    assert not any(getattr(h, "_lode_file", False) for h in new)

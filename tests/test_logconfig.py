"""Tests for lode.logconfig — application logging setup (lode-txh.4).

Asserts the acceptance criterion that logging is configured: the level resolves
from an explicit arg, then ``LODE_LOG_LEVEL``, then a sensible default, and
configuring sets the root logger level.
"""

import logging

import pytest

from lode.logconfig import LOG_LEVEL_ENV, configure_logging, resolve_level


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

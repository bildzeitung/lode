"""Application logging setup for lode (lode-txh.4).

One place to configure the stdlib root logger so lode's own logs and the
Anthropic SDK's logs (which propagate to the root logger) share a format and
destination. lode's own level is read from ``LODE_LOG_LEVEL`` (default ``INFO``)
or passed explicitly.

The Anthropic SDK has its *own* switch for wire-level debug output -- set
``ANTHROPIC_LOG=debug`` (or ``info``) and it logs on the ``anthropic`` logger,
which this configuration formats and routes alongside everything else.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

#: Env var lode reads for its own log level when one isn't passed explicitly.
LOG_LEVEL_ENV = "LODE_LOG_LEVEL"

_DEFAULT_LEVEL = "INFO"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: Log file name written inside the log directory ($LODE_HOME/logs/, docs/configuration.md).
_LOG_FILE = "lode.log"


def resolve_level(level: str | int | None = None) -> int:
    """Resolve a numeric log level from an arg, then ``LODE_LOG_LEVEL``, then INFO.

    Accepts a case-insensitive level name (e.g. ``"debug"``) or an int. An
    unrecognized name raises :class:`ValueError` so a typo fails loudly rather
    than silently defaulting.
    """
    if level is None:
        level = os.environ.get(LOG_LEVEL_ENV, _DEFAULT_LEVEL)
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.strip().upper())
    if not isinstance(resolved, int):
        raise ValueError(f"unknown log level: {level!r}")
    return resolved


def configure_logging(
    level: str | int | None = None, log_dir: Path | None = None
) -> int:
    """Configure the root logger and set its level; return the numeric level.

    ``basicConfig`` attaches the stderr handler only on the first call
    (idempotent), but the level is set on every call so a later call can raise or
    lower verbosity. When ``log_dir`` is given, a file handler is (re)attached
    writing to ``<log_dir>/lode.log`` (the directory is created if absent), so
    lode's logs land under ``$LODE_HOME/logs/`` (docs/configuration.md "Paths &
    locations") while still echoing to stderr.
    """
    resolved = resolve_level(level)
    logging.basicConfig(level=resolved, format=_LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(resolved)
    if log_dir is not None:
        _attach_file_handler(root, Path(log_dir), resolved)
    return resolved


def _attach_file_handler(root: logging.Logger, log_dir: Path, level: int) -> None:
    """Point lode's single file handler at ``<log_dir>/lode.log`` (idempotent).

    Any lode file handler already attached is closed and removed first, so
    repeated calls re-point at the current directory rather than accumulate open
    files — only one file handler is ever live.
    """
    for handler in list(root.handlers):
        if getattr(handler, "_lode_file", False):
            root.removeHandler(handler)
            handler.close()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / _LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.setLevel(level)
    handler._lode_file = True  # type: ignore[attr-defined]
    root.addHandler(handler)

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


class _ConsoleHttpxFilter(logging.Filter):
    """Drop httpx's sub-WARNING records (the "HTTP Request: ... 200 OK" line).

    Attached to the console/stream handler only (lode-1gr.7) -- the file
    handler is untouched, so ``lode.log`` still records every httpx request
    for debugging, while the console stays quiet for successful requests.
    httpx WARNING+ (e.g. retries, errors) still reaches the console.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name.startswith("httpx") and record.levelno < logging.WARNING
        )


def _add_console_httpx_filter(handler: logging.Handler) -> None:
    """Attach :class:`_ConsoleHttpxFilter` to ``handler`` unless already present."""
    if not any(isinstance(f, _ConsoleHttpxFilter) for f in handler.filters):
        handler.addFilter(_ConsoleHttpxFilter())


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
    level: str | int | None = None,
    log_dir: Path | None = None,
    *,
    console: bool = True,
) -> int:
    """Configure the root logger and set its level; return the numeric level.

    ``basicConfig`` attaches the stderr handler only on the first call
    (idempotent), but the level is set on every call so a later call can raise or
    lower verbosity. When ``log_dir`` is given, a file handler is (re)attached
    writing to ``<log_dir>/lode.log`` (the directory is created if absent), so
    lode's logs land under ``$LODE_HOME/logs/`` (docs/configuration.md "Paths &
    locations") while still echoing to stderr.

    ``console=False`` is the TUI's file-only mode (lode-1i8.2): instead of
    installing a stream handler, any stream/console handler already on the
    root logger (e.g. one the group callback's earlier ``console=True`` call
    installed via ``basicConfig``) is removed, so nothing echoes to the
    terminal and corrupts Textual's alternate-screen display. ``log_dir`` is
    required in this mode, and the file handler is attached *before* any
    stream handler is removed, so the root logger is never left
    handler-less -- which would let Python's ``logging.lastResort`` (WARNING+
    straight to stderr) fire and reintroduce the very corruption this guards
    against. Plain CLI commands never pass ``console=False``, so ``ask``/
    ``add``/etc. keep mirroring to stderr unchanged.

    In ``console=True`` mode, every non-file handler on the root logger (the
    stream handler ``basicConfig`` installs) gets a filter that drops httpx's
    sub-WARNING records -- the "HTTP Request: ... 200 OK" line the Anthropic
    SDK's httpx transport logs at INFO (lode-1gr.7). The root logger's own
    level is unchanged (still ``resolved``, e.g. INFO), so the file handler
    keeps recording httpx 200s for debugging; only the console is quieted,
    and httpx WARNING+/errors still reach it.
    """
    if not console and log_dir is None:
        raise ValueError("configure_logging: log_dir is required when console=False")
    resolved = resolve_level(level)
    root = logging.getLogger()
    if console:
        logging.basicConfig(level=resolved, format=_LOG_FORMAT)
        for handler in root.handlers:
            if not getattr(handler, "_lode_file", False):
                _add_console_httpx_filter(handler)
    root.setLevel(resolved)
    if log_dir is not None:
        _attach_file_handler(root, Path(log_dir), resolved)
    if not console:
        for handler in list(root.handlers):
            if not getattr(handler, "_lode_file", False):
                root.removeHandler(handler)
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

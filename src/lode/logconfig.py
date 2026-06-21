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

#: Env var lode reads for its own log level when one isn't passed explicitly.
LOG_LEVEL_ENV = "LODE_LOG_LEVEL"

_DEFAULT_LEVEL = "INFO"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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


def configure_logging(level: str | int | None = None) -> int:
    """Configure the root logger and set its level; return the numeric level.

    ``basicConfig`` attaches a handler only on the first call (idempotent), but
    the level is set on every call so a later call can raise or lower verbosity.
    """
    resolved = resolve_level(level)
    logging.basicConfig(level=resolved, format=_LOG_FORMAT)
    logging.getLogger().setLevel(resolved)
    return resolved

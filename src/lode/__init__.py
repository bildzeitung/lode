"""lode — an AI-first, TUI-first personal knowledge base.

The design source of truth lives under ``docs/``; this package is the
implementation skeleton (build sequencing: ``docs/design.md`` §7).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lode")
except PackageNotFoundError:
    # Raw source tree, never installed (not even editable) — see docs/release.md.
    __version__ = "0.0.0+unknown"

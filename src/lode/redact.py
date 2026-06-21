"""redact-before-index + redact-before-egress (lode-fk8.2).

``docs/externals.md`` ("Two redactions, aimed at the right legs") splits secret
redaction into two controls that hit different legs of the system:

- **redact-before-index** — keep a pasted ``.env`` / API key out of the *local*
  vector + FTS index so it never becomes locally retrievable. A local-at-rest
  concern; the secret still lives in ``versions.body`` (only ``purge`` removes
  that durable copy).
- **redact-before-egress** — strip known secret patterns from the **enrichment
  payload and the Q&A context** before they are sent to Claude. This is the
  control that actually limits cloud exposure, and it is a *precondition for any
  live Claude call*.

Both are the SAME primitive: pure **text-in / text-out** substitution of a
high-precision seed pattern set (``docs/configuration.md`` "Privacy & egress",
seeded in :data:`lode.config._SECRET_SEED_PATTERNS`) with a fixed
:data:`REDACTION_MARKER`. The two controls read SEPARATE config fields
(``redact_before_index_patterns`` / ``redact_before_egress_patterns``) so an
operator can tune them apart, but ship seeded identically.

This is the shared egress gate that E6 (``lode-az0.4``) and E7
(``lode-npx.1``/``.2``) consume. It is deliberately pure ``str -> str`` and does
not reach into the store, retrieval, enrichment, or Q&A subsystems — those
callers wire it in; it is implemented here once.
"""

import re
from collections.abc import Sequence
from functools import lru_cache

from lode.config import Settings

REDACTION_MARKER = "[redacted]"
"""Replacement written in place of every matched secret span."""


@lru_cache(maxsize=None)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Compile (and memoize) a pattern set; patterns are validated at config load."""
    return tuple(re.compile(pattern) for pattern in patterns)


def redact(text: str, patterns: Sequence[str]) -> str:
    """Replace every match of any ``patterns`` regex in ``text`` with the marker.

    Patterns are applied in order; since the marker contains no secret pattern,
    redaction is idempotent (re-running over already-redacted text is a no-op).
    An empty pattern set returns ``text`` unchanged.
    """
    for regex in _compiled(tuple(patterns)):
        text = regex.sub(REDACTION_MARKER, text)
    return text


def redact_before_index(text: str, settings: Settings | None = None) -> str:
    """Strip secrets from ``text`` before it is chunked/embedded into the local index."""
    settings = settings or Settings()
    return redact(text, settings.redact_before_index_patterns)


def redact_before_egress(text: str, settings: Settings | None = None) -> str:
    """Strip secrets from a payload before it is sent to Claude (enrich / Q&A)."""
    settings = settings or Settings()
    return redact(text, settings.redact_before_egress_patterns)

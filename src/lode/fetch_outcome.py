"""Connector-neutral HTTP fetch-outcome classifier (lode-gpzn.13).

Extracted from :mod:`lode.webfetch` (the web draw-down connector, lode-w0h.1)
so the Atlassian connectors (JIRA lode-gpzn.3, Confluence lode-gpzn.4) reuse
one classifier instead of each reimplementing the same status-code mapping.
This is a **prerequisite refactor** (bd lode-gpzn.13, filed via ``/challenge``
of lode-gpzn, owner decision C) — behavior-preserving for the existing web
path; :mod:`lode.webfetch` re-points onto this module rather than keeping its
own copy of the constants/decision.

## The classifier's scope — HTTP status only

This module carries **only** the HTTP-specific half of the fetch-outcome
taxonomy: what a response's status code alone implies about retryability.
It does **not** cover the "2xx but no real content" tombstone case (an empty/
too-short readability extraction) — that signal is trafilatura-specific and
stays in :mod:`lode.webfetch`, since a non-HTML connector response (JIRA/
Confluence JSON) has no readability-extraction step of this shape. A
non-HTTP connector is out of scope entirely — all three connectors here
(web, JIRA Cloud, Confluence Cloud) speak HTTP.

## Taxonomy (decision, bd lode-w0h.1, debate round 3, 2026-07-08; see
``docs/externals.md`` "Fetch-outcome taxonomy" for the full picture
including the non-HTTP-status cases)

- **OK** — 2xx/3xx (``< 400``): the caller proceeds (reads the body, follows
  a redirect, etc).
- **TRANSIENT** — 408 Request Timeout, 429 Too Many Requests, or any 5xx:
  retrying might help. The caller is expected to raise/propagate so the
  async work queue's existing attempts/backoff/dead-letter machinery
  (``failed`` → ``pending`` retry, → ``dead`` at ``retry_max_attempts``,
  PINNED lode-i05.6) retries it.
- **TOMBSTONE** — any other ``>= 400`` (401/403/404/410/...): retrying an
  identical request yields an identical response, so this is permanent.

408/429 are carved out of the general 4xx-is-permanent rule because RFC 9110
§15.5.9 itself flags 408 as "the client MAY repeat the request", and 429 is
the standard rate-limit signal — both are the server explicitly saying "try
again", unlike every other 4xx.
"""

from __future__ import annotations

from enum import Enum

#: The 4xx codes HTTP itself flags as "try again later" — everything else in
#: the 4xx range is a permanent tombstone. 408 Request Timeout (RFC 9110
#: §15.5.9: "the client MAY repeat the request") and 429 Too Many Requests.
TRANSIENT_4XX = frozenset({408, 429})

#: At and above this, every status is a 5xx server error — always retryable.
TRANSIENT_STATUS_FLOOR = 500


class HttpOutcome(str, Enum):
    """Outcome of classifying one HTTP status code — see module docstring."""

    OK = "ok"
    TOMBSTONE = "tombstone"
    TRANSIENT = "transient"


def classify_http_status(status_code: int) -> HttpOutcome:
    """Classify ``status_code`` per the shared HTTP fetch-outcome taxonomy.

    Connector-neutral: takes only a bare status code, no fetcher/response
    object, so any HTTP-speaking connector can call it with nothing more
    than the number it already has. See the module docstring for the three
    outcomes' meaning and the 408/429 carve-out rationale.
    """
    if status_code in TRANSIENT_4XX or status_code >= TRANSIENT_STATUS_FLOOR:
        return HttpOutcome.TRANSIENT
    if status_code >= 400:
        return HttpOutcome.TOMBSTONE
    return HttpOutcome.OK


__all__ = [
    "TRANSIENT_4XX",
    "TRANSIENT_STATUS_FLOOR",
    "HttpOutcome",
    "classify_http_status",
]

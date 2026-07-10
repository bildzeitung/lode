"""URL detection, explicit edges, and the one-hop web draw-down trigger (lode-w0h.3).

The trigger that turns note capture into connector activity (E12 web draw-down
connector). Two halves, both wired atomically into the note-save path
(:meth:`lode.repository.Repository.save`) so a crash between them never leaves
a saved note with an orphaned URL:

- **Synchronous (this module, called from ``save``):** scan the just-saved
  body for pasted URLs, canonicalize each to a stable ``external_id``, create
  a ``source='user'`` edge ``note_id -> external_id`` (docs/externals.md
  "Edges: explicit vs inferred" — explicit, never a gated suggestion), and
  enqueue exactly one ``refresh`` job per newly-linked external. No network
  I/O happens here — the enqueue is a plain INSERT on the caller's
  transaction, same shape as :func:`lode.jobs.enqueue_derive_jobs`'s own
  embed/enrich enqueue. **Fetching the page is never inline with the save.**
- **Asynchronous (the ``refresh`` job handler, run later by the worker):**
  :func:`refresh_external` does the actual network fetch
  (:func:`lode.webfetch.fetch_and_extract`) and write
  (:func:`lode.externals.ingest_fetch_result`) — this is where the queued
  draw-down actually happens.

## Shared job type (decision, bd lode-w0h.3, debate round 3, 2026-07-08)

The draw-down job **reuses the ``refresh`` enum value already reserved** on
``jobs.type`` (``schema.sql``: ``CHECK (type IN ('embed', 'enrich',
'refresh'))``) — no schema migration. This module is what *introduces the
handler* for it (:func:`refresh_external`, registered in
:mod:`lode.worker` as ``"refresh"``): the paste-triggered initial draw-down is
just the *first* refresh of a source, riding the exact same
attempts/backoff/dead-letter machinery (``failed`` -> ``pending`` retry, ->
``dead`` at ``settings.retry_max_attempts``, PINNED lode-i05.6) that any later
re-fetch would. ``lode-w0h.6`` (refresh policy: TTL / on-access revalidation)
reuses :func:`refresh_external` unchanged and adds only staleness detection +
scheduling — not a second fetch path.

## URL canonicalization (decision, bd lode-w0h.3, debate round 3, 2026-07-08)

A web ``external_id`` **is** its canonical URL string (not a hash — see
docs/externals.md "External identity": "normalized URL" is one of the
`external_id` shapes) — so this canonicalization *is* the dedup correctness
the ticket's acceptance ("same URL in two notes = one external node") depends
on, and the exact same join key ``lode-w0h.6``'s refresh policy will use to
find "the same source" across refetches. :func:`canonicalize_url` applies, in
order:

1. Lowercase the scheme and host (``urlsplit().hostname`` already lowercases;
   the path/query are left as-cased — servers may treat those
   case-sensitively).
2. **Drop userinfo** (``user[:pass]@``) **entirely** (lode-0as, 2026-07-09,
   PRIVACY fix). Credentials in a pasted URL are transport secrets, not
   source identity — the same reasoning that strips ``utm_*`` below, applied
   to the strongest instance of that class. ``external_id`` is a durable
   identifier that propagates into ``edges.to_id``, the retrieval candidate
   set, and any UI that prints a source URL, and it is *not* covered by hard
   delete (docs/externals.md "Hard delete" scrubs ``versions.body``, not an
   identifier already copied into edges/indexes) — so a credential must never
   enter it in the first place. ``https://user:pass@host/p``,
   ``https://user@host/p``, and ``https://host/p`` all canonicalize
   identically.
3. Strip the port when it equals the scheme's default (``:80`` for ``http``,
   ``:443`` for ``https``).
4. Drop the fragment (``#...``) entirely — never part of server-side
   identity.
5. Strip query params matching the tracking blocklist
   (``settings.url_tracking_param_blocklist``, docs/configuration.md;
   default ``utm_*``, ``fbclid``, ``gclid``).
6. Sort the remaining ``(key, value)`` query pairs.
7. Normalize the trailing slash: an empty or bare ``/`` path becomes ``/``;
   any other path loses a trailing ``/`` (so ``/foo`` and ``/foo/`` collapse
   to one canonical form).

Path percent-encoding (``%7E`` vs ``~``) and IDN hosts (unicode vs punycode)
are **deliberately left unnormalized** — a decision, not an oversight; see
docs/externals.md "URL canonicalization" for why these two are out of scope
here (they are a dedup-correctness gap, not a privacy one, and normalizing
them safely needs care this ticket didn't need to take on).

## The redirect wrinkle (decision, bd lode-w0h.3, debate round 3, 2026-07-08)

A note's edge is created against the *pasted* URL's canonical form **before**
any fetch runs (fetching is the async job this same save enqueues). If that
fetch follows a 3xx chain to a different final URL
(:attr:`lode.webfetch.FetchResult.final_url`), the snapshot is ingested under
the *final* URL's canonical ``external_id`` instead of the pasted one, and
:func:`refresh_external` re-points every ``source='user'`` edge that was
asserted against the pasted-canonical id onto the final-canonical id — so the
note's edge always resolves to the external node that actually holds content,
and a persistently-redirecting URL still dedups onto one node rather than
minting a dangling ``externals`` row for the pre-redirect id.

## One-hop enforcement

Only :func:`detect_and_enqueue_drawdown` ever calls :func:`extract_urls` — it
runs once, from the note-save path, over the *note's own* body.
:func:`refresh_external` never calls it over a fetched snapshot's body, so the
fetched page's own outbound links are never scanned or drawn down
(docs/externals.md "Draw-down rules": "recursion = unbounded web crawler, not
a notes app"). This is structural, not a counter to check against a limit.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lode import jobs
from lode.config import Settings
from lode.externals import ingest_fetch_result
from lode.webfetch import Fetcher, fetch_and_extract

log = logging.getLogger(__name__)

#: ``externals.source_type`` for every web-draw-down node (matches the value
#: already used in lode-w0h.2's own tests).
SOURCE_TYPE_WEB = "web"

#: One http(s) URL run: no whitespace, angle brackets, or quotes (the
#: characters most likely to be prose delimiters around a pasted URL, not
#: legal in an unencoded URL anyway).
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

#: Trailing characters commonly adjacent to a pasted URL in prose (sentence-
#: ending punctuation, closing brackets/quotes) that are not part of the URL
#: itself. A trailing ``)`` gets special handling (see :func:`extract_urls`)
#: since a legitimate URL path segment can itself contain balanced parens
#: (e.g. a Wikipedia "Foo_(bar)" page).
_TRAILING_STRIP = ".,;:!?)]}”’»"

#: The scheme's implicit port — stripped by :func:`canonicalize_url` when the
#: URL spells it out explicitly (``http://host:80/`` == ``http://host/``).
_DEFAULT_PORTS = {"http": 80, "https": 443}


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------


def extract_urls(body: str) -> list[str]:
    """Extract distinct http(s) URLs from ``body``, in first-seen order.

    A deliberately narrow heuristic for a personal-notes paste, not a
    general-purpose URL extractor: matches ``https?://`` runs of non-
    whitespace/angle-bracket/quote characters, then trims trailing prose
    punctuation that is not part of the URL (the period in "see
    https://example.com." or the wrapping parens in
    "(https://example.com)").

    A trailing ``)`` is kept when the matched URL's own open-paren count is
    ``>=`` its close-paren count — i.e. the parens are balanced or the URL
    legitimately contains more opens than closes within its own text (e.g.
    ``.../wiki/Foo_(bar)``) — and stripped otherwise (an extra, unmatched
    closing paren wrapping the URL from surrounding prose).

    Deduped on the *literal* matched string; two different-looking URLs that
    canonicalize to the same ``external_id`` are deduped later, by
    :func:`detect_and_enqueue_drawdown`'s edge-existence check, not here.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(body):
        url = match.group(0)
        while url and url[-1] in _TRAILING_STRIP:
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            url = url[:-1]
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def _is_tracking_param(name: str, settings: Settings) -> bool:
    """Match ``name`` against ``settings.url_tracking_param_blocklist``.

    A blocklist entry ending in ``*`` is a case-insensitive prefix match
    (``"utm_*"`` matches ``utm_source``, ``utm_medium``, ...); any other
    entry must match ``name`` exactly (case-insensitive).
    """
    lname = name.lower()
    for pattern in settings.url_tracking_param_blocklist:
        if pattern.endswith("*"):
            if lname.startswith(pattern[:-1].lower()):
                return True
        elif lname == pattern.lower():
            return True
    return False


def canonicalize_url(url: str, settings: Settings | None = None) -> str:
    """Canonicalize ``url`` into its stable ``external_id`` form.

    See the module docstring's "URL canonicalization" section for the exact,
    pinned rule set (lowercase scheme+host, strip default port, drop
    fragment, strip tracking params, sort remaining query params, normalize
    trailing slash). Raises ``ValueError`` if ``url`` cannot be parsed (e.g.
    an unparseable port) — callers on the note-save path
    (:func:`detect_and_enqueue_drawdown`) catch this per-URL so one malformed
    pasted URL cannot fail the whole save.
    """
    settings = settings or Settings()
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    # Userinfo (``user[:pass]@``) is dropped entirely, never carried into
    # netloc — see the module docstring's "URL canonicalization" §2
    # (lode-0as): credentials are transport secrets, not source identity,
    # and must never enter external_id.
    netloc = (parts.hostname or "").lower()
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{parts.port}"

    path = parts.path
    if path in ("", "/"):
        path = "/"
    elif path.endswith("/"):
        path = path.rstrip("/") or "/"

    query_pairs = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(key, settings)
    )
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


# ---------------------------------------------------------------------------
# Note-save trigger: explicit edge + refresh enqueue (synchronous, no network)
# ---------------------------------------------------------------------------


def detect_and_enqueue_drawdown(
    conn: sqlite3.Connection,
    note_id: str,
    version_id: str,
    body: str,
    *,
    settings: Settings | None = None,
) -> list[str]:
    """Create explicit edges + enqueue draw-down for every URL pasted in ``body``.

    Called from :meth:`lode.repository.Repository.save`, inside the same
    ``with conn:`` transaction as the version write (no network I/O here —
    just the edge INSERT and the job enqueue, both plain rows on the
    caller's connection). For each URL :func:`extract_urls` finds:

    1. Canonicalize it to an ``external_id`` (:func:`canonicalize_url`); a
       URL that fails to canonicalize is logged and skipped rather than
       failing the whole save.
    2. If a ``source='user'`` edge ``note_id -> external_id`` already exists
       (this note previously linked the same canonical URL — including via
       a *different*-looking but equivalent raw URL string), skip both the
       edge insert and the enqueue: this note is already linked to that
       external, and the "same URL in two notes = one node, two edges"
       acceptance is about distinct *notes*, not repeated saves of the same
       note. A different note linking the same canonical URL still gets its
       own edge (and, if the prior draw-down already finished, its own fresh
       ``refresh`` job — a plain refetch, free if unchanged per
       docs/externals.md "Snapshot churn").
    3. Otherwise, insert the edge (``source='user'``, high confidence,
       ``quoted_text`` = the literal pasted URL for provenance — never
       re-anchored, since ``source='user'`` edges are irreplaceable per
       :mod:`lode.staleness`) and enqueue exactly one ``refresh`` job keyed
       on ``external_id`` (idempotent-by-key via ``idx_jobs_live``: a second
       paste of the same still-live URL, before the first refresh drains,
       enqueues nothing new).

    Returns the canonical ``external_id`` for every URL detected in ``body``
    (deduped within this call), whether or not it was newly linked — mostly
    useful for tests/logging, not required by any caller today.
    """
    settings = settings or Settings()
    external_ids: list[str] = []
    for url in extract_urls(body):
        try:
            external_id = canonicalize_url(url, settings)
        except ValueError:
            log.warning("drawdown: could not canonicalize pasted URL %r — skipped", url)
            continue
        if external_id in external_ids:
            continue
        external_ids.append(external_id)

        exists = conn.execute(
            "SELECT 1 FROM edges WHERE from_id = ? AND to_id = ? AND source = 'user' LIMIT 1",
            (note_id, external_id),
        ).fetchone()
        if exists:
            continue

        conn.execute(
            "INSERT INTO edges "
            "(from_id, to_id, source, reason, confidence, source_version, "
            "quoted_text, status) "
            "VALUES (?, ?, 'user', ?, 1.0, ?, ?, 'fresh')",
            (note_id, external_id, "pasted URL", version_id, url),
        )
        jobs.enqueue_derive_jobs(conn, external_id, types=("refresh",))
        log.debug("drawdown: linked %s -> %s, enqueued refresh", note_id, external_id)

    return external_ids


# ---------------------------------------------------------------------------
# Refresh job handler: fetch + ingest (async, the actual network I/O)
# ---------------------------------------------------------------------------


def _repoint_edges(
    conn: sqlite3.Connection, old_external_id: str, new_external_id: str
) -> int:
    """Re-point every ``source='user'`` edge from ``old_external_id`` to ``new_external_id``.

    The redirect-wrinkle fix (see module docstring): only ``source='user'``
    edges are touched — the only edges this module itself creates — never
    ``source='ai'`` rows. Returns the count re-pointed (0 in the common,
    no-redirect case). Idempotent: re-running after the first successful
    re-point finds no rows left at ``old_external_id`` and is a no-op.
    """
    with conn:
        cur = conn.execute(
            "UPDATE edges SET to_id = ? WHERE to_id = ? AND source = 'user'",
            (new_external_id, old_external_id),
        )
    return cur.rowcount


def refresh_external(
    conn: sqlite3.Connection,
    target_external_id: str,
    settings: Settings,
    *,
    fetcher: Fetcher | None = None,
) -> str | None:
    """Fetch + ingest ``target_external_id`` — the shared ``refresh`` handler.

    ``target_external_id`` is itself a canonical, directly-fetchable URL (an
    ``external_id`` for a web source *is* its canonical form — see module
    docstring). This is the one fetch->ingest path both the paste-triggered
    initial draw-down and ``lode-w0h.6``'s later refresh policy ride:

    1. :func:`lode.webfetch.fetch_and_extract` the URL. A
       :class:`~lode.webfetch.TransientFetchError` is **not** caught here —
       it propagates to the caller (:mod:`lode.worker`'s ``run_one``, which
       already treats any raised exception with the same
       attempts/backoff/dead-letter accounting), so a transient failure
       rides the existing retry machinery with no special-casing needed.
    2. Canonicalize :attr:`~lode.webfetch.FetchResult.final_url` — usually
       identical to ``target_external_id``, but different after a followed
       3xx redirect (the "redirect wrinkle", module docstring).
    3. :func:`lode.externals.ingest_fetch_result` under the *final*
       canonical id (never the pre-redirect one), then re-point any
       ``source='user'`` edges still pointing at the pre-redirect id
       (:func:`_repoint_edges`) — a no-op when there was no redirect.

    ``fetcher`` is the same injectable seam :mod:`lode.webfetch` already
    defines (production omits it; :func:`fetch_and_extract` then builds the
    real :class:`~lode.webfetch.HttpxFetcher`) — this module adds no new
    network seam of its own.

    Returns a one-line human-readable outcome (mirrors the ``embed``/
    ``enrich`` handler convention, ``lode-1gr.4``) for ``lode work``'s echo.
    """
    result = fetch_and_extract(target_external_id, fetcher=fetcher, settings=settings)
    final_external_id = canonicalize_url(result.final_url, settings)

    ingest = ingest_fetch_result(
        conn, final_external_id, SOURCE_TYPE_WEB, result, settings=settings
    )

    repointed = 0
    if final_external_id != target_external_id:
        repointed = _repoint_edges(conn, target_external_id, final_external_id)

    outcome = f"refreshed {target_external_id}: {ingest.status}"
    if final_external_id != target_external_id:
        outcome += f" -> {final_external_id} (repointed {repointed} edge(s))"
    return outcome


__all__ = [
    "SOURCE_TYPE_WEB",
    "canonicalize_url",
    "detect_and_enqueue_drawdown",
    "extract_urls",
    "refresh_external",
]

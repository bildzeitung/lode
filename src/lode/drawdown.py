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

## Atlassian link detection + source_type routing (lode-gpzn.2)

Before a pasted URL falls through to the generic web path above,
:func:`detect_and_enqueue_drawdown` checks it against the JIRA/Confluence
Cloud shapes below — synchronously, no network I/O (owner decision F,
``/challenge`` 2026-07-17), so this step never blocks the note-save
transaction on an auth round-trip:

- **Host match:** the pasted URL's host is checked against
  ``settings.jira_base_url``/``confluence_base_url`` when configured, else
  inferred from the ``*.atlassian.net`` Cloud shape — but only when the
  product is *active* (:func:`lode.config.jira_active` /
  :func:`~lode.config.confluence_active`: flagged on AND credentials
  resolve). Flag-off or unresolved credentials means no Atlassian match is
  ever attempted, so the URL falls straight through to the unchanged web
  path (locked decision 5, bd lode-gpzn epic).
- **JIRA:** only the canonical ``/browse/{KEY}`` permalink shape carries an
  issue key; anything else on a matched JIRA host (dashboards, boards, ...)
  has no semantic id to route on and falls through to the web path too.
- **Confluence:** only an id-bearing URL (``/wiki/spaces/SPACE/pages/{id}/
  ...``) routes — a tiny-link (``/wiki/x/AbCdE``) or legacy
  (``/display/SPACE/Title``) form carries no page-id in the URL itself, and
  resolving one would need an API round-trip this synchronous step must not
  make (owner decision F). Both fall through to today's generic web path
  (login page => tombstone), exactly like flag-off.

A match yields ``(source_type, external_id, api_base)`` — ``external_id`` is
now the **semantic** key (the issue key / page id), not a URL, so it is no
longer directly fetchable the way a web ``external_id`` is. The inferred-or-
configured ``api_base`` is therefore **persisted synchronously on the
``externals`` row at detection** (owner decision A) — a new ``api_base``
column (``src/lode/schema.sql``) — so the async ``refresh`` job (still the
one shared job type, no new ``jobs.type`` value) can rebuild
``{api_base}+{external_id}`` without ever having seen the original URL. Two
different URL forms of the same issue/page (e.g. reached via a configured
base vs the inferred ``*.atlassian.net`` host) parse to the same semantic
key, so they dedup to one ``externals`` row exactly like two equivalent web
URLs dedup via :func:`canonicalize_url`.

:func:`refresh_external` is the dispatcher this persisted ``source_type``
drives (see its own docstring below) — a real refactor of what used to be a
single web-only handler, not free reuse (owner decision B, bd lode-gpzn.2
notes).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from lode import jobs
from lode.config import Settings, confluence_active, jira_active
from lode.confluence import fetch_confluence_page
from lode.externals import ingest_fetch_result
from lode.jira_fetch import fetch_jira_issue
from lode.webfetch import Fetcher, FetchResult, fetch_and_extract

log = logging.getLogger(__name__)

#: ``externals.source_type`` for every web-draw-down node (matches the value
#: already used in lode-w0h.2's own tests).
SOURCE_TYPE_WEB = "web"

#: ``externals.source_type`` for a JIRA Cloud issue (lode-gpzn.2). The fetch
#: unit that actually calls the JIRA REST API is built in lode-gpzn.3.
SOURCE_TYPE_JIRA = "jira"

#: ``externals.source_type`` for a Confluence Cloud page (lode-gpzn.2). The
#: fetch unit that actually calls the Confluence REST API is built in
#: lode-gpzn.4.
SOURCE_TYPE_CONFLUENCE = "confluence"

#: JIRA Cloud's canonical issue permalink shape ("copy link" on an issue):
#: ``/browse/{PROJECT}-{NUMBER}``. Any other path on a matched JIRA host (a
#: board, a dashboard, a search) carries no semantic id and is left to the
#: web path.
_JIRA_ISSUE_RE = re.compile(r"^/browse/([A-Za-z][A-Za-z0-9]*-\d+)/?$")

#: Confluence Cloud's id-bearing page URL shape:
#: ``/wiki/spaces/{SPACE}/pages/{id}/{title-slug}``. Deliberately does NOT
#: match a tiny-link (``/wiki/x/AbCdE``) or a legacy display URL
#: (``/display/{SPACE}/{Title}``) — neither carries a page-id in the URL
#: itself (owner decision F); both fall through to the web path.
_CONFLUENCE_PAGE_RE = re.compile(r"^/wiki/spaces/[^/]+/pages/(\d+)(?:/.*)?$")


def _host_matches(hostname: str, configured_base: str) -> bool:
    """True if ``hostname`` (already lowercased) is this product's Cloud host.

    Matches the configured base URL's host when one is set
    (``settings.jira_base_url`` / ``confluence_base_url``); otherwise infers
    the Cloud shape (``*.atlassian.net``) — the same "configured override,
    else infer" rule :func:`lode.config.Settings.jira_base_url` documents.
    """
    if configured_base:
        return hostname == (urlsplit(configured_base).hostname or "").lower()
    return hostname.endswith(".atlassian.net")


def _resolve_api_base(parts: SplitResult, configured_base: str) -> str:
    """The API base to persist on the ``externals`` row for a matched URL.

    The configured override (trailing slash stripped, for clean
    ``{api_base}+{external_id}`` concatenation by the gpzn.3/gpzn.4 fetch
    units) when one is set; otherwise the pasted URL's own scheme+host —
    the inferred Cloud base.
    """
    if configured_base:
        return configured_base.rstrip("/")
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}"


def _classify_atlassian(url: str, settings: Settings) -> tuple[str, str, str] | None:
    """Classify ``url`` as a routable JIRA/Confluence Cloud link, or ``None``.

    Returns ``(source_type, external_id, api_base)`` on a match — see the
    module docstring's "Atlassian link detection" section for the exact
    rules. ``None`` means: not a matched, active, id-bearing Atlassian link,
    so the caller falls through to the unchanged web path (flag-off,
    unresolved credentials, a non-Atlassian host, or an Atlassian host with
    no parseable semantic id all land here).
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return None

    if jira_active(settings) and _host_matches(hostname, settings.jira_base_url):
        match = _JIRA_ISSUE_RE.match(parts.path)
        if match:
            return (
                SOURCE_TYPE_JIRA,
                match.group(1),
                _resolve_api_base(parts, settings.jira_base_url),
            )

    if confluence_active(settings) and _host_matches(
        hostname, settings.confluence_base_url
    ):
        match = _CONFLUENCE_PAGE_RE.match(parts.path)
        if match:
            return (
                SOURCE_TYPE_CONFLUENCE,
                match.group(1),
                _resolve_api_base(parts, settings.confluence_base_url),
            )

    return None


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
    # ``urlsplit().hostname`` strips the brackets from an IPv6 literal host
    # (``[::1]`` -> ``::1``) -- re-add them before appending a port, or the
    # rebuilt netloc (``::1:8080``) is ambiguous/unparseable (the host's own
    # colons get read as the port delimiter). A bracketed host is the only
    # one containing ``:`` at this point (userinfo and port are already
    # stripped out of ``hostname``), so this check is unambiguous (lode-lt1).
    if ":" in netloc:
        netloc = f"[{netloc}]"
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

    1. Classify it (:func:`_classify_atlassian`). A matched, active
       JIRA/Confluence Cloud link yields its semantic ``external_id`` (an
       issue key / page id) and persists its ``source_type`` + ``api_base``
       on the ``externals`` row synchronously (owner decision A, lode-
       gpzn.2 — see the module docstring's "Atlassian link detection"
       section). Anything else — including a matched host with no
       parseable id, or Atlassian routing flagged off/unresolved — falls
       through to the unchanged web path: canonicalize it to an
       ``external_id`` (:func:`canonicalize_url`); a URL that fails to
       canonicalize is logged and skipped rather than failing the whole
       save.
    2. If a ``source='user'`` edge ``note_id -> external_id`` already exists
       (this note previously linked the same canonical URL/semantic key —
       including via a *different*-looking but equivalent raw URL string),
       skip both the edge insert and the enqueue: this note is already
       linked to that external, and the "same URL in two notes = one node,
       two edges" acceptance is about distinct *notes*, not repeated saves
       of the same note. A different note linking the same canonical
       URL/semantic key still gets its own edge (and, if the prior
       draw-down already finished, its own fresh ``refresh`` job — a plain
       refetch, free if unchanged per docs/externals.md "Snapshot churn").
    3. Otherwise, insert the edge (``source='user'``, high confidence,
       ``quoted_text`` = the literal pasted URL for provenance — never
       re-anchored, since ``source='user'`` edges are irreplaceable per
       :mod:`lode.staleness`) and enqueue exactly one ``refresh`` job keyed
       on ``external_id`` (idempotent-by-key via ``idx_jobs_live``: a second
       paste of the same still-live URL, before the first refresh drains,
       enqueues nothing new).

    Returns the ``external_id`` for every URL detected in ``body`` (deduped
    within this call), whether or not it was newly linked — mostly useful
    for tests/logging, not required by any caller today.
    """
    settings = settings or Settings()
    external_ids: list[str] = []
    for url in extract_urls(body):
        atlassian = _classify_atlassian(url, settings)
        if atlassian is not None:
            source_type, external_id, api_base = atlassian
        else:
            try:
                external_id = canonicalize_url(url, settings)
            except ValueError:
                log.warning(
                    "drawdown: could not canonicalize pasted URL %r — skipped", url
                )
                continue
            source_type = None
            api_base = None

        if external_id in external_ids:
            continue
        external_ids.append(external_id)

        exists = conn.execute(
            "SELECT 1 FROM edges WHERE from_id = ? AND to_id = ? AND source = 'user' LIMIT 1",
            (note_id, external_id),
        ).fetchone()
        if exists:
            continue

        if source_type is not None:
            # Owner decision A: persist source_type + api_base on the
            # externals row NOW, synchronously — the async refresh handler
            # can no longer derive them from external_id alone once
            # external_id is a semantic key rather than a URL. ON CONFLICT
            # DO NOTHING mirrors lode.externals.ingest_snapshot's own
            # externals upsert: first-write-wins, idempotent for a second
            # note linking the same already-known external.
            conn.execute(
                "INSERT INTO externals (external_id, source_type, api_base) "
                "VALUES (?, ?, ?) ON CONFLICT (external_id) DO NOTHING",
                (external_id, source_type, api_base),
            )

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


def _refresh_web(
    conn: sqlite3.Connection,
    target_external_id: str,
    settings: Settings,
    *,
    fetcher: Fetcher | None = None,
) -> str | None:
    """Fetch + ingest a web ``target_external_id`` — the ``SOURCE_TYPE_WEB`` leg.

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


def _refresh_atlassian(
    conn: sqlite3.Connection,
    external_id: str,
    settings: Settings,
    *,
    fetch_fn: Callable[..., FetchResult],
    source_type: str,
    fetcher: Fetcher | None = None,
) -> str | None:
    """Fetch + ingest a JIRA/Confluence Cloud item -- the shared Atlassian leg (lode-40zj).

    ``_refresh_jira`` (lode-gpzn.3) and ``_refresh_confluence``
    (lode-gpzn.4/lode-mfts) had byte-identical bodies once both landed —
    the ``api_base`` SELECT, the missing-``api_base`` ``RuntimeError`` guard,
    the fetch call, :func:`~lode.externals.ingest_fetch_result`, and the
    ``"refreshed {id}: {status}"`` return — differing only in the fetch
    callable (:func:`~lode.jira_fetch.fetch_jira_issue` vs
    :func:`~lode.confluence.fetch_confluence_page`, both sharing the
    ``(external_id, api_base, *, fetcher, settings) -> FetchResult``
    signature) and the ``SOURCE_TYPE_*`` constant. This is that one body,
    parameterized on both — ``refresh_external`` still keeps its own
    explicit ``if``/``elif`` dispatch (not a registry — speculative
    flexibility this ticket doesn't need).

    Unlike the web leg, ``external_id`` here is a semantic key (an issue key
    / page id), not itself a fetchable URL — the request URL is rebuilt
    inside ``fetch_fn`` from the ``api_base`` persisted on the ``externals``
    row at link-detection time (lode-gpzn.2). No redirect/repoint step is
    needed here (unlike :func:`_refresh_web`): a semantic key has no
    redirect concept, and :attr:`~lode.webfetch.FetchResult.final_url` is
    purely informational for this leg — :func:`~lode.externals.
    ingest_fetch_result` never reads it.
    """
    row = conn.execute(
        "SELECT api_base FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    api_base = row[0] if row is not None else None
    if not api_base:
        raise RuntimeError(
            f"_refresh_atlassian: no api_base persisted for external_id={external_id!r}"
        )

    result = fetch_fn(external_id, api_base, fetcher=fetcher, settings=settings)
    ingest = ingest_fetch_result(
        conn, external_id, source_type, result, settings=settings
    )
    return f"refreshed {external_id}: {ingest.status}"


def refresh_external(
    conn: sqlite3.Connection,
    target_external_id: str,
    settings: Settings,
    *,
    fetcher: Fetcher | None = None,
) -> str | None:
    """The shared ``refresh`` job handler — dispatches by ``externals.source_type``.

    **A real refactor, not free reuse (owner decision B, bd lode-gpzn.2):**
    before lode-gpzn.2, this function *was* the web fetch leg, hardcoded —
    ``source_type`` was a data column written but never branched on, since
    ``"web"`` was the only value anything ever wrote. lode-gpzn.2's detection
    step (:func:`_classify_atlassian`, called from
    :func:`detect_and_enqueue_drawdown`) is the first thing that writes
    ``"jira"``/``"confluence"``, so this now looks the row up and routes:

    - **No ``externals`` row yet, or ``source_type == SOURCE_TYPE_WEB``:**
      the unchanged web fetch leg (:func:`_refresh_web`) —
      ``target_external_id`` is itself the fetchable URL. "No row yet" is
      the common case for a *first* web refresh: :func:`detect_and_
      enqueue_drawdown` never pre-creates a web external's row (only
      :func:`lode.externals.ingest_snapshot`, on the first successful
      fetch, does) — see :func:`lode.worker._refresh_dead_letter_hook`'s
      matching fallback for the same reasoning.
    - **``source_type == SOURCE_TYPE_JIRA``:** the JIRA Cloud REST fetch
      unit (:func:`_refresh_atlassian` with
      :func:`~lode.jira_fetch.fetch_jira_issue`, lode-gpzn.3).
    - **``source_type == SOURCE_TYPE_CONFLUENCE``:** the Confluence Cloud
      REST fetch unit (:func:`_refresh_atlassian` with
      :func:`~lode.confluence.fetch_confluence_page`, lode-gpzn.4).
    - **Anything else:** an unrecognized ``source_type`` raises
      ``RuntimeError`` naming both the value and the ``external_id`` —
      reachable only via direct DB tampering or a future connector adding
      a value here before its dispatch leg exists.

    ``fetcher`` is passed straight through to :func:`_refresh_web` /
    :func:`_refresh_atlassian`.
    """
    row = conn.execute(
        "SELECT source_type FROM externals WHERE external_id = ?",
        (target_external_id,),
    ).fetchone()
    source_type = row[0] if row is not None else SOURCE_TYPE_WEB

    if source_type == SOURCE_TYPE_WEB:
        return _refresh_web(conn, target_external_id, settings, fetcher=fetcher)
    if source_type == SOURCE_TYPE_JIRA:
        return _refresh_atlassian(
            conn,
            target_external_id,
            settings,
            fetch_fn=fetch_jira_issue,
            source_type=SOURCE_TYPE_JIRA,
            fetcher=fetcher,
        )
    if source_type == SOURCE_TYPE_CONFLUENCE:
        return _refresh_atlassian(
            conn,
            target_external_id,
            settings,
            fetch_fn=fetch_confluence_page,
            source_type=SOURCE_TYPE_CONFLUENCE,
            fetcher=fetcher,
        )
    raise RuntimeError(
        f"refresh_external: unknown source_type={source_type!r} "
        f"external_id={target_external_id!r}"
    )


__all__ = [
    "SOURCE_TYPE_CONFLUENCE",
    "SOURCE_TYPE_JIRA",
    "SOURCE_TYPE_WEB",
    "canonicalize_url",
    "detect_and_enqueue_drawdown",
    "extract_urls",
    "refresh_external",
]

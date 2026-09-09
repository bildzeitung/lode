"""JIRA backfill: migrate pre-existing JIRA links to the API connector (lode-gpzn.10).

Plugs the JIRA connector into the backfill-command framework (:mod:`lode.backfill`,
``lode-gpzn.9``) — encapsulated here, in the JIRA connector's own module, not in
shared detection code (the framework deliberately ships no connector logic of
its own).

## What gets migrated

A link pasted **before** the JIRA connector existed (or was flagged on) drew
down through the generic web path: a plain scrape, or — the common case for an
auth-fronted JIRA instance — a login-page scrape that tombstoned. That link's
``externals`` row is ``source_type='web'``, keyed on the *canonicalized URL*,
not the semantic issue key the connector now uses
(:mod:`lode.drawdown`'s "Atlassian link detection" — owner decision 3).

:func:`_jira_backfill` re-runs :func:`lode.drawdown._classify_atlassian` — the exact
same synchronous, network-free classifier
:func:`lode.drawdown.detect_and_enqueue_drawdown` itself uses at paste time — against
every existing explicit (``source='user'``) edge's ``quoted_text`` (the literal
originally- pasted URL, preserved verbatim by :func:`lode.drawdown._repoint_edges`
across any prior repoint). This is deliberate reuse, not a second, drifting copy of the
JIRA URL-matching rules: the backfill must classify a link *exactly* the way live
draw-down would, "under CURRENT connector routing" (the framework's own charter,
``lode.backfill``'s module docstring) — including the current flag/credential state
(:func:`lode.config.jira_active`) and the current
``jira_base_url``/inferred-``*.atlassian.net`` host rule. A link that still doesn't
classify (flag off, no credentials, non-Atlassian host, or an Atlassian host with no
``/browse/{KEY}`` shape) is left untouched — exactly what live draw-down would do with
it today.

## Composed entirely from the framework's shared plumbing

Mirrors the reference shape in ``tests/test_backfill.py``'s
``_fake_atlassian_backfill`` (written alongside the framework itself,
lode-gpzn.9, to foreshadow this exact connector): iterate, mint, repoint,
gate-on-``needs_refresh``, enqueue — no hand-rolled SQL of this module's own.

## Bare JIRA issue keys (lode-2o45)

A bare key (``PROJ-42``, no URL) predating ``jira_projects`` listing its
prefix — or predating the whole feature — was never linked at save time (the
detector didn't run yet), so unlike a URL there is no ``source='user'`` edge
to reclassify for it. :func:`_bare_jira_key_backfill` closes that gap by
scanning every live note head's body directly with the exact same detector
:func:`lode.drawdown.detect_and_enqueue_drawdown` uses at save time
(:func:`~lode.drawdown._bare_jira_scan_active` +
:func:`~lode.drawdown.iter_bare_jira_key_spans`, with the same URL-span
overlap exclusion) and links + enqueues any match the note doesn't already
have an edge for. Idempotent: a key already linked from a given note is
skipped by the edge-existence check, so a second pass finds nothing new to
do. :func:`_classify_bare_jira_key` (used by :func:`_jira_backfill`'s
existing-edge loop below) is the same helper the save path's bare-key
routing gates on — reused, not reimplemented — for the narrower case of an
edge whose ``quoted_text`` is already a bare key rather than a URL.

## Idempotent re-run, including the tombstone-exclusion override

Every linked edge is reclassified from its **original** ``quoted_text`` on
every pass, not filtered by the edge's *current* ``source_type`` — so a
second (or later) run correctly revisits an already-migrated edge (now
``source_type='jira'``, ``external_id`` already the semantic key) instead of
silently losing track of it once the first pass repoints it away from
``'web'``. Two consequences:

- **First migration**: ``link.external_id`` (still the old canonicalized URL)
  differs from the freshly classified semantic key, so
  :func:`~lode.backfill.mint_external` + :func:`~lode.backfill.repoint_edges` run once;
  the fresh,
  never-tombstoned identity always passes :func:`~lode.backfill.needs_refresh`
  (owner decision D — no override needed here).
- **Later re-run**: ``link.external_id`` already equals the semantic key, so
  mint/repoint are skipped (nothing left to migrate for that edge) and only
  :func:`~lode.backfill.needs_refresh` is re-checked — the one place
  ``retry_tombstoned`` is ever load-bearing, exactly the re-run-over-an-
  already-tombstoned-target case the framework's own override exists for.

``dry_run`` threads straight through to every shared-plumbing call, per the
framework's own dry-run contract — this module keeps no bookkeeping of its
own.
"""

from __future__ import annotations

import sqlite3

from lode.backfill import (
    enqueue_fresh_refresh,
    iter_user_linked_externals,
    mint_external,
    needs_refresh,
    register_backfill,
    repoint_edges,
)
from lode.config import Settings
from lode.drawdown import (
    SOURCE_TYPE_JIRA,
    _bare_jira_scan_active,
    _classify_atlassian,
    _classify_bare_jira_key,
    iter_bare_jira_key_spans,
    iter_url_spans,
)


def _jira_backfill(
    conn: sqlite3.Connection,
    settings: Settings,
    dry_run: bool,
    retry_tombstoned: bool,
) -> str:
    """The registered ``"jira"`` :data:`lode.backfill.BackfillHandler`.

    Two passes (module docstring, "Bare JIRA issue keys"): reclassify every
    existing explicit edge under current routing (URL or already-linked
    bare key), then scan live note bodies directly for a bare key that was
    never linked in the first place. Returns a one-line human-readable
    summary — the outcome-line convention
    :func:`lode.drawdown.refresh_external` / ``lode work`` already use.
    """
    migrated = 0
    refreshed = 0
    for link in iter_user_linked_externals(conn):
        if not link.quoted_text:
            continue
        classified = _classify_atlassian(link.quoted_text, settings)
        if classified is None:
            classified = _classify_bare_jira_key(link.quoted_text, settings)
        if classified is None or classified[0] != SOURCE_TYPE_JIRA:
            continue
        _, key, api_base = classified

        if link.external_id != key:
            # First migration for this edge: mint the fresh semantic
            # identity and re-point the edge onto it.
            if mint_external(
                conn,
                key,
                SOURCE_TYPE_JIRA,
                api_base,
                settings=settings,
                dry_run=dry_run,
            ):
                migrated += 1
            repoint_edges(conn, link.external_id, key, dry_run=dry_run)

        if needs_refresh(conn, key, retry_tombstoned=retry_tombstoned):
            enqueue_fresh_refresh(conn, key, dry_run=dry_run)
            refreshed += 1

    bare_linked, bare_refreshed = _bare_jira_key_backfill(
        conn, settings, dry_run=dry_run, retry_tombstoned=retry_tombstoned
    )
    migrated += bare_linked
    refreshed += bare_refreshed

    return (
        f"jira backfill: migrated {migrated} link(s), enqueued {refreshed} refresh(es)"
    )


def _bare_jira_key_backfill(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    dry_run: bool,
    retry_tombstoned: bool,
) -> tuple[int, int]:
    """Scan every live note head's body for an un-linked bare JIRA key and link it.

    See the module docstring's "Bare JIRA issue keys" section. Mirrors
    :func:`lode.reconcile._embed_gap_step`'s own live-head query shape
    (``head_version_id`` joined to ``versions``, excluding a soft-deleted
    or hard-purged head) rather than inventing a second one. Returns
    ``(linked, refreshed)`` counts, folded into :func:`_jira_backfill`'s
    own summary.
    """
    if not _bare_jira_scan_active(settings):
        return 0, 0

    api_base = settings.jira_base_url.rstrip("/")
    linked = 0
    refreshed = 0
    rows = conn.execute(
        """
        SELECT n.note_id, n.head_version_id, v.body
        FROM notes n
        JOIN versions v ON v.version_id = n.head_version_id
        WHERE n.head_version_id IS NOT NULL
          AND v.op != 'delete'
          AND v.purged_at IS NULL
        """
    ).fetchall()
    for note_id, version_id, body in rows:
        url_spans = [(start, end) for start, end, _ in iter_url_spans(body)]
        for key_start, key_end, key in iter_bare_jira_key_spans(
            body, settings.jira_projects
        ):
            if any(
                key_start < url_end and key_end > url_start
                for url_start, url_end in url_spans
            ):
                continue  # already routed via the URL form -- see it above

            exists = conn.execute(
                "SELECT 1 FROM edges WHERE from_id = ? AND to_id = ? "
                "AND source = 'user' LIMIT 1",
                (note_id, key),
            ).fetchone()
            if exists:
                continue  # already linked from THIS note -- fully idempotent

            mint_external(
                conn,
                key,
                SOURCE_TYPE_JIRA,
                api_base,
                settings=settings,
                dry_run=dry_run,
            )
            if not dry_run:
                with conn:
                    conn.execute(
                        "INSERT INTO edges "
                        "(from_id, to_id, source, reason, confidence, "
                        "source_version, quoted_text, status) "
                        "VALUES (?, ?, 'user', ?, 1.0, ?, ?, 'fresh')",
                        (note_id, key, "bare JIRA key", version_id, key),
                    )
            linked += 1

            if needs_refresh(conn, key, retry_tombstoned=retry_tombstoned):
                enqueue_fresh_refresh(conn, key, dry_run=dry_run)
                refreshed += 1

    return linked, refreshed


def register() -> None:
    """Register :func:`_jira_backfill` under the name ``"jira"``.

    Deliberately a **function**, not a bare module-level
    ``register_backfill(...)`` statement — ``lode backfill``
    (``src/lode/cli/backfill.py``) calls it explicitly on every invocation.
    The rationale is stated once, in
    :func:`lode.confluence_backfill.register`'s own docstring; this connector
    follows the identical pattern. Calling it more than once is always safe
    (:func:`~lode.backfill.register_backfill` is a plain dict assignment).
    """
    register_backfill("jira", _jira_backfill)


__all__ = ["register"]

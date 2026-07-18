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

:func:`_jira_backfill` re-runs :func:`lode.drawdown._classify_atlassian` — the
exact same synchronous, network-free classifier :func:`lode.drawdown.
detect_and_enqueue_drawdown` itself uses at paste time — against every existing
explicit (``source='user'``) edge's ``quoted_text`` (the literal originally-
pasted URL, preserved verbatim by :func:`lode.drawdown._repoint_edges` across
any prior repoint). This is deliberate reuse, not a second, drifting copy of
the JIRA URL-matching rules: the backfill must classify a link *exactly* the
way live draw-down would, "under CURRENT connector routing" (the framework's
own charter, ``lode.backfill``'s module docstring) — including the current
flag/credential state (:func:`lode.config.jira_active`) and the current
``jira_base_url``/inferred-``*.atlassian.net`` host rule. A link that still
doesn't classify (flag off, no credentials, non-Atlassian host, or an
Atlassian host with no ``/browse/{KEY}`` shape) is left untouched — exactly
what live draw-down would do with it today.

## Composed entirely from the framework's shared plumbing

Mirrors the reference shape in ``tests/test_backfill.py``'s
``_fake_atlassian_backfill`` (written alongside the framework itself,
lode-gpzn.9, to foreshadow this exact connector): iterate, mint, repoint,
gate-on-``needs_refresh``, enqueue — no hand-rolled SQL of this module's own.

## Idempotent re-run, including the tombstone-exclusion override

Every linked edge is reclassified from its **original** ``quoted_text`` on
every pass, not filtered by the edge's *current* ``source_type`` — so a
second (or later) run correctly revisits an already-migrated edge (now
``source_type='jira'``, ``external_id`` already the semantic key) instead of
silently losing track of it once the first pass repoints it away from
``'web'``. Two consequences:

- **First migration**: ``link.external_id`` (still the old canonicalized URL)
  differs from the freshly classified semantic key, so :func:`~lode.backfill.
  mint_external` + :func:`~lode.backfill.repoint_edges` run once; the fresh,
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
from lode.drawdown import SOURCE_TYPE_JIRA, _classify_atlassian


def _jira_backfill(
    conn: sqlite3.Connection,
    settings: Settings,
    dry_run: bool,
    retry_tombstoned: bool,
) -> str:
    """The registered ``"jira"`` :data:`lode.backfill.BackfillHandler`.

    Returns a one-line human-readable summary — the outcome-line convention
    :func:`lode.drawdown.refresh_external` / ``lode work`` already use.
    """
    migrated = 0
    refreshed = 0
    for link in iter_user_linked_externals(conn):
        if not link.quoted_text:
            continue
        classified = _classify_atlassian(link.quoted_text, settings)
        if classified is None or classified[0] != SOURCE_TYPE_JIRA:
            continue
        _, key, api_base = classified

        if link.external_id != key:
            # First migration for this edge: mint the fresh semantic
            # identity and re-point the edge onto it.
            if mint_external(conn, key, SOURCE_TYPE_JIRA, api_base, dry_run=dry_run):
                migrated += 1
            repoint_edges(conn, link.external_id, key, dry_run=dry_run)

        if needs_refresh(conn, key, retry_tombstoned=retry_tombstoned):
            enqueue_fresh_refresh(conn, key, dry_run=dry_run)
            refreshed += 1

    return (
        f"jira backfill: migrated {migrated} link(s), enqueued {refreshed} refresh(es)"
    )


# Register the "jira" connector on module load -- mirrors
# lode.reconcile's own register_step(...) calls at module load. See
# src/lode/cli.py's top-level `import lode.jira_backfill` for *why* this
# module is imported eagerly (not lazily, unlike lode.backfill/lode.reconcile
# themselves) -- test determinism under pytest-xdist, not a style change.
register_backfill("jira", _jira_backfill)

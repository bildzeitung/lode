"""Confluence backfill: migrate pre-existing Confluence links to the API connector (lode-gpzn.11).

Plugs Confluence into the backfill-command framework (``lode.backfill``,
lode-gpzn.9) — encapsulated here, in the Confluence connector's own module,
not in the framework's shared detection code, mirroring the JIRA backfill's
identical shape (lode-gpzn.10).

## What "pre-existing" means here

Before lode-gpzn.2's Atlassian link detection existed (or before an operator
flagged Confluence on / configured credentials), a pasted Confluence URL fell
through to the generic web path: canonicalized as a plain ``source_type =
'web'`` external, keyed on the URL string itself (docs/externals.md "Draw-
down rules"). If that page required auth, the web fetch typically tombstoned
(login page, or a 401/403). :func:`backfill_confluence` re-detects any such
already-processed, still ``web``-typed link that is *actually* a Confluence
Cloud page URL under **current** routing, and re-draws it down through the
real Confluence connector instead.

## Detection: reused, not reimplemented

Re-classifying a link is exactly the same judgment
:func:`lode.drawdown.detect_and_enqueue_drawdown` already makes on a fresh
paste — so this handler reuses :func:`lode.drawdown._classify_atlassian`
verbatim (the same cross-module private-helper reuse
:mod:`lode.backfill` itself already established for
:func:`~lode.drawdown._repoint_edges`) against each ``web``-typed edge's
``quoted_text`` (the literal originally-pasted URL), rather than re-deriving
the Confluence host-match / id-bearing-path regex a second time here.
Reusing it also means this handler automatically honors the exact same
"active" gate ``detect_and_enqueue_drawdown`` does — if Confluence isn't
currently flagged on with resolved credentials,
:func:`~lode.drawdown._classify_atlassian` returns ``None`` for every URL,
so a backfill run finds nothing to migrate. That is the correct behavior for
"re-run draw-down for a connector's already-processed links under **current**
routing" (docs/externals.md): an operator is expected to have already
flagged Confluence on before running this backfill.

Only ``web``-typed links are considered — a link already migrated (its edge
now points at a ``confluence``-typed external, per a prior backfill pass or
a fresh post-lode-gpzn.2 paste) is not ``web``-typed anymore and is skipped,
which is what makes a full re-run of this handler naturally idempotent (see
``tests/test_backfill.py``'s own end-to-end reference handler and its
``test_rerun_over_tombstoned_target_needs_override`` — the same structural
fact applies here: a second full pass can't rediscover an already-repointed
edge, so ``--retry-tombstoned``'s one load-bearing case is a re-run over a
target whose *migration* already happened but whose *refresh* tombstoned,
not a second full backfill sweep finding the link again).

## Shared plumbing composed, no hand-rolled SQL

Every write goes through :mod:`lode.backfill`'s four shared pieces
(:func:`~lode.backfill.mint_external`, :func:`~lode.backfill.repoint_edges`,
:func:`~lode.backfill.needs_refresh`, :func:`~lode.backfill.enqueue_fresh_refresh`) —
this module owns only the Confluence-specific detection step, per the framework's
"reused, not reimplemented per connector" contract (docs/externals.md "Backfill:
per-connector re-draw-down").
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
from lode.drawdown import SOURCE_TYPE_CONFLUENCE, SOURCE_TYPE_WEB, _classify_atlassian


def backfill_confluence(
    conn: sqlite3.Connection,
    settings: Settings,
    dry_run: bool,
    retry_tombstoned: bool,
) -> str:
    """Re-detect ``web``-typed Confluence links and migrate them to the connector.

    Registered under the name ``"confluence"`` (:func:`register`) — this is
    the handler :func:`lode.backfill.run_backfill` dispatches to for
    ``lode backfill confluence``. See the module docstring for the full
    detection + migration shape. Returns a one-line summary, per the
    framework's own handler contract.
    """
    migrated = 0
    for link in iter_user_linked_externals(conn):
        if link.source_type != SOURCE_TYPE_WEB or not link.quoted_text:
            continue
        classified = _classify_atlassian(link.quoted_text, settings)
        if classified is None or classified[0] != SOURCE_TYPE_CONFLUENCE:
            continue
        _, external_id, api_base = classified

        mint_external(
            conn, external_id, SOURCE_TYPE_CONFLUENCE, api_base, dry_run=dry_run
        )
        repoint_edges(conn, link.external_id, external_id, dry_run=dry_run)
        if needs_refresh(conn, external_id, retry_tombstoned=retry_tombstoned):
            enqueue_fresh_refresh(conn, external_id, dry_run=dry_run)
            migrated += 1

    return f"migrated {migrated} link(s)"


def register() -> None:
    """Register :func:`backfill_confluence` under the name ``"confluence"``.

    Deliberately a **function**, not a bare module-level
    ``register_backfill(...)`` statement: ``lode backfill`` (``src/lode/
    cli/backfill.py``) calls this explicitly on every invocation, because a bare
    module-level call would only ever fire once per process (Python's
    import caching means the module body doesn't re-execute on a second
    ``import``) — which is fine for a script that runs once, but not for a
    CLI whose in-process test suite re-invokes the command many times
    against a registry an autouse fixture deliberately clears before each
    test (``tests/test_cli_backfill.py``'s ``_clean_registry``).
    :func:`~lode.backfill.register_backfill` is a plain dict assignment, so
    calling this more than once is always safe.
    """
    register_backfill("confluence", backfill_confluence)


__all__ = ["backfill_confluence", "register"]

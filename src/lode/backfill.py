"""Backfill command capability: per-connector re-draw-down framework (lode-gpzn.9).

A **framework**, not a sweep. Sooner or later a connector changes how an
already-processed link resolves — the flagship case: a URL that drew down
through the generic web path (login page => tombstone, or a plain scrape)
before a connector (e.g. Atlassian, ``lode-gpzn``) existed or was flagged on,
and now should route through that connector's structured fetch instead. This
module owns the CLI-triggered machinery to *re-run* draw-down for such links
under **current** routing — but deliberately does **not** decide *which*
links need it for any given connector; that classification is per-connector
judgment (``lode-gpzn.10``/``lode-gpzn.11`` and beyond), plugged in through
the registry below.

**CLI-only, explicitly no TUI surface** — this runs per-machine, wherever the
notes' DB lives (``$LODE_HOME``), and does not travel on the Dolt/git wire.

## The registry seam

A connector registers a :data:`BackfillHandler` under a short name (e.g.
``"jira"``) via :func:`register_backfill`, the same shape
:func:`lode.reconcile.register_step` already uses for the reconciliation
scan's step registry. ``lode backfill <name>`` (``src/lode/cli/backfill.py``) resolves
the handler via :func:`run_backfill` and prints whatever one-line summary the
handler returns — mirroring the outcome-line convention
:func:`lode.drawdown.refresh_external` / ``lode work`` already use, rather
than this framework inventing a second reporting shape.

No connector is registered by this ticket — the registry starts empty, and
``lode backfill`` (no argument, or ``--list``) reports that plainly instead of
erroring. ``lode-gpzn.10``/``.11`` are what actually call
:func:`register_backfill`.

## Shared plumbing — reused, not reimplemented per connector

A connector's handler is expected to compose these four pieces rather than
hand-roll its own version of any of them (the ticket's own acceptance:
"the shared re-point/enqueue plumbing lives in the framework and is reused,
not reimplemented per connector"):

1. :func:`iter_user_linked_externals` — walk every existing explicit
   (``source='user'``) note -> external edge, the "already-processed link"
   unit a connector re-classifies under its current routing.
2. :func:`mint_external` — INSERT a fresh ``externals`` row for a newly
   computed identity (first-write-wins, mirrors
   :func:`lode.drawdown.detect_and_enqueue_drawdown`'s own externals upsert).
3. :func:`repoint_edges` — re-point every ``source='user'`` edge from the old
   identity to the new one. Reuses :func:`lode.drawdown._repoint_edges`
   verbatim (the exact function the redirect-wrinkle case already uses,
   ``docs/externals.md`` "The redirect wrinkle") rather than a second
   hand-rolled ``UPDATE``.
4. :func:`enqueue_fresh_refresh` (gated by :func:`needs_refresh`) — enqueue
   exactly one fresh ``refresh`` job for the new identity via the single
   shared enqueue path (:func:`lode.jobs.enqueue_derive_jobs`).

Every one of the four accepts a ``dry_run`` flag (mint/repoint/enqueue) so
``lode backfill --dry-run`` reports what *would* change without writing
anything — a connector handler threads the CLI's ``--dry-run`` straight
through rather than needing its own dry-run bookkeeping.

## Tombstone-exclusion override — re-run idempotency only (owner decision D)

A per-connector backfill **mints a brand-new, never-tombstoned** semantic
external for its first migration (see point 2 above) — so on the *first*
pass, :func:`needs_refresh` always returns ``True`` without ever consulting
``retry_tombstoned``: there is nothing tombstoned yet to exclude.

The override matters only on an **idempotent re-run** where the *new*
identity's own head snapshot already tombstoned on a prior backfill pass
(e.g. a bad token => 401, permanently failed under the old attempt). Left at
its default (``retry_tombstoned=False``), :func:`needs_refresh` mirrors
:func:`lode.reconcile`'s own ``s.status != 'tombstone'`` predicate (the
``embed_gap``/``refresh_stale`` steps, ``docs/externals.md`` "Refresh
policy": a tombstone is a *permanent*-failure record a periodic sweep must
never blindly retry). ``lode backfill --retry-tombstoned`` is the explicit,
human-driven opt-in past that default — for the one case a periodic sweep
structurally can't cover: an operator who has just fixed the underlying
cause (rotated the token, fixed the base URL) and wants this specific
already-tombstoned target retried now, not on a schedule.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from lode import jobs
from lode.config import Settings, default_settings_for_missing_arg
from lode.drawdown import _repoint_edges
from lode.externals import _insert_external

#: A connector's registered backfill handler:
#: ``(conn, settings, dry_run, retry_tombstoned) -> summary``.
#: ``dry_run`` and ``retry_tombstoned`` are the CLI's own flags, threaded
#: straight through — see the module docstring's "Shared plumbing" and
#: "Tombstone-exclusion override" sections for what each controls. The
#: returned ``str`` is a one-line human-readable summary printed as-is by
#: ``lode backfill`` (the same outcome-line convention
#: :func:`lode.drawdown.refresh_external` uses).
BackfillHandler = Callable[[sqlite3.Connection, Settings, bool, bool], str]

#: Module-level registry, populated by :func:`register_backfill` — mirrors
#: :data:`lode.reconcile._STEPS`'s shape. Empty until a connector (e.g.
#: ``lode-gpzn.10``/``.11``) registers into it; this ticket ships no
#: registrant of its own.
_REGISTRY: dict[str, BackfillHandler] = {}


class BackfillError(Exception):
    """Raised by :func:`run_backfill` for an unregistered connector name."""


def register_backfill(name: str, handler: BackfillHandler) -> None:
    """Register ``handler`` under ``name`` — the registry seam a connector's
    own backfill module calls at import time (mirrors
    :func:`lode.reconcile.register_step`)."""
    _REGISTRY[name] = handler


def registered_backfills() -> list[str]:
    """Sorted list of currently-registered connector names."""
    return sorted(_REGISTRY)


def run_backfill(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    *,
    dry_run: bool = False,
    retry_tombstoned: bool = False,
) -> str:
    """Dispatch to ``name``'s registered handler; return its summary line.

    Raises :class:`BackfillError` — naming every currently-registered
    connector, so the message is actionable rather than a bare "not found" —
    if ``name`` has no registered handler.
    """
    try:
        handler = _REGISTRY[name]
    except KeyError:
        available = ", ".join(registered_backfills()) or "(none registered)"
        raise BackfillError(
            f"no backfill connector registered as {name!r}; available: {available}"
        ) from None
    return handler(conn, settings, dry_run, retry_tombstoned)


# ---------------------------------------------------------------------------
# Shared plumbing (module docstring, "Shared plumbing" section)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkedExternal:
    """One existing explicit (``source='user'``) note -> external edge.

    The "already-processed link" unit a connector's backfill handler
    iterates over and re-classifies under its current routing.
    """

    note_id: str
    external_id: str
    source_type: str
    quoted_text: str | None


def iter_user_linked_externals(conn: sqlite3.Connection) -> Iterator[LinkedExternal]:
    """Yield every existing explicit (``source='user'``) note -> external edge.

    Joined to the external's current ``source_type`` and the edge's own
    ``quoted_text`` (the literal originally-pasted URL,
    :func:`lode.drawdown.detect_and_enqueue_drawdown`) — the two pieces a
    connector's handler needs to decide whether a given link should now
    route differently. Excludes nothing else on its own (not even
    ``source_type='web'``): a connector handler is responsible for filtering
    to the shape it cares about (e.g. only ``web`` rows whose ``quoted_text``
    now matches a newly-active host).

    ``source='ai'`` (inferred) edges are never included — a backfill re-runs
    *explicit* draw-down, exactly as the original paste-time trigger only
    ever created ``source='user'`` edges (``docs/externals.md`` "Edges:
    explicit vs inferred").
    """
    rows = conn.execute(
        """
        SELECT e.from_id, e.to_id, ext.source_type, e.quoted_text
        FROM edges e
        JOIN externals ext ON ext.external_id = e.to_id
        WHERE e.source = 'user'
        ORDER BY e.from_id, e.to_id
        """
    ).fetchall()
    for from_id, to_id, source_type, quoted_text in rows:
        yield LinkedExternal(
            note_id=from_id,
            external_id=to_id,
            source_type=source_type,
            quoted_text=quoted_text,
        )


def mint_external(
    conn: sqlite3.Connection,
    external_id: str,
    source_type: str,
    api_base: str | None = None,
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> bool:
    """INSERT a fresh ``externals`` row for ``external_id`` if none exists yet.

    First-write-wins (``ON CONFLICT DO NOTHING``) via
    :func:`lode.externals._insert_external`, mirroring
    :func:`lode.drawdown.detect_and_enqueue_drawdown`'s own externals upsert
    — idempotent for a second backfill pass over a link that already
    migrated, and seeding ``no_egress`` from ``Settings.no_egress_default``
    on true first-write only (lode-ge8w). Returns ``True`` iff a row was (or,
    under ``dry_run``, *would be*) newly inserted; ``False`` means the
    identity already exists (a prior pass already minted it).

    ``dry_run=True`` performs no write — it only reports whether a row would
    be inserted, per the module docstring's dry-run contract.
    """
    if dry_run:
        row = conn.execute(
            "SELECT 1 FROM externals WHERE external_id = ?", (external_id,)
        ).fetchone()
        return row is None
    settings = settings or default_settings_for_missing_arg("backfill.mint_external")
    with conn:
        return _insert_external(
            conn, external_id, source_type, settings, api_base=api_base
        )


def repoint_edges(
    conn: sqlite3.Connection,
    old_external_id: str,
    new_external_id: str,
    *,
    dry_run: bool = False,
) -> int:
    """Re-point every ``source='user'`` edge ``old_external_id -> new_external_id``.

    Reuses :func:`lode.drawdown._repoint_edges` verbatim — the exact
    function the redirect-wrinkle case already rides
    (``docs/externals.md`` "The redirect wrinkle") — rather than a second,
    parallel ``UPDATE``. Returns the count re-pointed (or, under
    ``dry_run``, the count that *would be*).

    ``dry_run=True`` performs no write.
    """
    if dry_run:
        row = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE to_id = ? AND source = 'user'",
            (old_external_id,),
        ).fetchone()
        return row[0] if row else 0
    return _repoint_edges(conn, old_external_id, new_external_id)


def needs_refresh(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    retry_tombstoned: bool = False,
) -> bool:
    """Whether ``external_id`` should get a fresh ``refresh`` job right now.

    See the module docstring's "Tombstone-exclusion override" section for
    the full rationale. In one line: ``True`` unless ``external_id`` already
    has a head snapshot and that snapshot is a tombstone, in which case the
    result is exactly ``retry_tombstoned`` — so the override is only ever
    load-bearing on that one re-run-over-a-tombstoned-target case; a first
    migration (no snapshot yet) is unaffected by ``retry_tombstoned`` either
    way.
    """
    row = conn.execute(
        """
        SELECT s.status
        FROM externals e
        JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id
        WHERE e.external_id = ?
        """,
        (external_id,),
    ).fetchone()
    if row is None:
        return True
    (status,) = row
    return status != "tombstone" or retry_tombstoned


def enqueue_fresh_refresh(
    conn: sqlite3.Connection, external_id: str, *, dry_run: bool = False
) -> None:
    """Enqueue exactly one fresh ``refresh`` job for ``external_id``.

    Rides the single shared enqueue path
    (:func:`lode.jobs.enqueue_derive_jobs`, ``types=("refresh",)``) — the
    same one the paste-time trigger and the ``refresh_stale`` reconciliation
    step both use. ``ON CONFLICT DO NOTHING`` against ``idx_jobs_live`` means
    a target with an already-pending/running ``refresh`` job is a no-op, so
    calling this more than once for the same target is always safe.

    ``dry_run=True`` performs no write.
    """
    if dry_run:
        return
    with conn:
        jobs.enqueue_derive_jobs(conn, external_id, types=("refresh",))


__all__ = [
    "BackfillError",
    "BackfillHandler",
    "LinkedExternal",
    "enqueue_fresh_refresh",
    "iter_user_linked_externals",
    "mint_external",
    "needs_refresh",
    "register_backfill",
    "registered_backfills",
    "repoint_edges",
    "run_backfill",
]

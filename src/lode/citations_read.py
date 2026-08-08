"""Citation identity + as-of resolution -- the shared read side both the TUI's
ask screen and ``lode ask`` need (lode-kuc7).

Relocated out of :mod:`lode.tui.services.ask` (where it was written for
lode-35nu.1, resolving each surviving citation's owning note/external and
as-of provenance in one batched pass): it is pure store I/O -- sqlite3 +
:class:`~lode.answer.Support`, no widget/App state -- so living under
``lode.tui`` meant :mod:`lode.cli`'s own ``ask`` command could not call it and
kept a hand-copied as-of-only mirror instead. Same rule
:mod:`lode.notes_read` documents for its own relocation (lode-1gr.1): "a CLI
command reading through ``lode.tui.*`` would have the dependency direction
backwards." This module depends on neither ``lode.tui`` nor ``lode.cli`` --
both depend on it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lode.notes_read import first_line
from lode.target_rows import fetch_target_rows

if TYPE_CHECKING:
    from lode.answer import Support


@dataclass(frozen=True)
class CitationIdentity:
    """Resolved note/external identity for one citation target (lode-35nu.1).

    Exactly one of ``note_id``/``external_id`` is set, mirroring
    :class:`~lode.answer.Support`'s own ``version_id``/``snapshot_id`` split.
    ``title`` is :func:`lode.notes_read.first_line` of the *cited*
    version/snapshot's body -- taken from the cited version even when that
    version is superseded, so an old citation shows the title it had rather
    than the current head's. ``is_head`` is True when the cited
    version/snapshot is still the note/external's current head.
    """

    title: str
    is_head: bool
    note_id: str | None = None
    external_id: str | None = None


def resolve_citations(
    conn: sqlite3.Connection, supports: list[Support]
) -> tuple[dict[str, str | None], dict[str, CitationIdentity], dict[str, str]]:
    """Resolve as-of provenance, identity, and body for every cited target, batched (lode-35nu.1, lode-35nu.3).

    Two queries total -- one ``IN (...)`` over every distinct cited
    ``version_id``, one over every distinct cited ``snapshot_id`` -- so a
    multi-claim answer costs a fixed two round-trips regardless of citation
    count (the ticket's "a single batched query" acceptance line). The
    batched pair itself is :func:`lode.target_rows.fetch_target_rows`, the
    same shared shape :func:`lode.cited_answer._resolve_targets` uses
    (lode-r9z0); this function keeps its own column list and its own
    row -> result mapping. The as-of
    stamp rides along on the same rows the identity comes from (a note version
    is stamped at write time, ``versions.created``; an external snapshot at
    fetch time, ``snapshots.fetched_at``), so it costs no extra query.
    ``bodies`` rides along the same way -- the body is already SELECTed for
    :func:`~lode.notes_read.first_line`, so surfacing it for the ask screen's
    context rendering around ``quoted_span`` (lode-35nu.3) costs nothing
    extra.

    Returns ``(as_of, identities, bodies)``, all keyed by
    :attr:`~lode.answer.Support.target_id`. Every cited target is a key in
    ``as_of``, mapping to ``None`` when the store had nothing to resolve; such
    a target is simply absent from ``identities``/``bodies``. Unresolvable is
    practically unreachable -- the faithfulness gate already verified the
    span against the stored body -- but handled rather than assumed away.
    """
    identities: dict[str, CitationIdentity] = {}
    as_of: dict[str, str | None] = {}
    bodies: dict[str, str] = {}

    version_ids = tuple({s.version_id for s in supports if s.version_id is not None})
    snapshot_ids = tuple({s.snapshot_id for s in supports if s.snapshot_id is not None})

    version_rows, snapshot_rows = fetch_target_rows(
        conn,
        version_ids,
        snapshot_ids,
        "v.version_id, v.note_id, v.body, v.created, n.head_version_id",
        "s.snapshot_id, s.external_id, s.body, s.fetched_at, e.head_snapshot_id",
    )

    for version_id, note_id, body, created, head_version_id in version_rows:
        identities[version_id] = CitationIdentity(
            title=first_line(body),
            is_head=version_id == head_version_id,
            note_id=note_id,
        )
        as_of[version_id] = created
        bodies[version_id] = body

    for snapshot_id, external_id, body, fetched_at, head_snapshot_id in snapshot_rows:
        identities[snapshot_id] = CitationIdentity(
            title=first_line(body),
            is_head=snapshot_id == head_snapshot_id,
            external_id=external_id,
        )
        as_of[snapshot_id] = fetched_at
        bodies[snapshot_id] = body

    for support in supports:
        as_of.setdefault(support.target_id, None)
    return as_of, identities, bodies

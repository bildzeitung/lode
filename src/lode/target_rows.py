"""Batched two-query fetch for the polymorphic ``(version_id | snapshot_id)``
target shape (lode-r9z0).

Several read paths resolve a set of cited/graph ``target_version`` ids that
are polymorphic over two disjoint id spaces -- a note's ``version_id`` or an
external's ``snapshot_id`` -- and want the result in **at most two round
trips** regardless of how many distinct targets are involved: one
``versions JOIN notes ... WHERE version_id IN (...)`` query, one
``snapshots JOIN externals ... WHERE snapshot_id IN (...)`` query
(:func:`lode.cited_answer._resolve_targets`, lode-ekqh;
:func:`lode.citations_read.resolve_citations`, lode-35nu.1/.3). This module
factors out exactly that shared shape -- split ids, build the ``IN(...)``
placeholder string, run the two queries -- and nothing else: each caller
supplies its own ``SELECT`` column list (as a raw SQL fragment) and does its
own row -> result mapping, since the two call sites want different columns
and produce different result shapes.

Deliberately **not** shared here: any per-row post-processing (e.g.
``no_egress`` composition with a scope ruleset) stays in the caller, since
that logic is call-site-specific and, per ``docs/no_egress_scope``, must not
grow a generic seam. ``lode.retrieval.trust_rank`` was evaluated as a third
caller and left alone; the full rationale (and the part of its shape that
*does* match) is in ``docs/decisions.md`` under lode-r9z0, not restated here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any


def fetch_target_rows(
    conn: sqlite3.Connection,
    note_ids: Sequence[str],
    external_ids: Sequence[str],
    note_columns: str,
    external_columns: str,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Run the batched note/external ``IN(...)`` pair, columns supplied by the caller.

    ``note_ids`` are looked up as ``versions.version_id`` (joined to
    ``notes``); ``external_ids`` are ``snapshots.snapshot_id`` values, *not*
    ``externals.external_id`` -- they are named for the side of the split
    they select, and ``e.external_id`` is a distinct column on the same row.

    ``note_columns``/``external_columns`` are raw SQL fragments for each
    query's ``SELECT`` list (e.g. ``"v.version_id, v.body, n.no_egress"``) --
    callers alias the tables ``v``/``n`` (note side) and ``s``/``e``
    (external side), matching the ``JOIN`` this function issues.

    **These two fragments are interpolated into the SQL, not bound.** They
    must be fixed literals written at the call site; never pass a value
    derived from note content, an external's body, config, or any other
    caller-supplied or user-supplied data through them. The *ids* are always
    parameterized (``?`` placeholders, bound below) and carry no such
    restriction -- the column lists are the only injection surface here, and
    keeping them literal is what closes it.

    The row tuples are shaped by whatever the caller asked for, so the return
    is deliberately ``tuple[Any, ...]``: the arity cannot be known here.

    Either id sequence may be empty, in which case that query is skipped
    entirely (an empty ``IN ()`` is invalid SQL and would also be a wasted
    round trip). Returns ``(note_rows, external_rows)`` as the raw
    ``fetchall()`` tuples for each query, in no particular order -- callers
    map rows back to their own result shape.
    """
    note_rows: list[tuple[Any, ...]] = []
    if note_ids:
        placeholders = ", ".join("?" for _ in note_ids)
        note_rows = conn.execute(
            f"SELECT {note_columns} FROM versions v "
            "JOIN notes n ON n.note_id = v.note_id "
            f"WHERE v.version_id IN ({placeholders})",
            tuple(note_ids),
        ).fetchall()

    external_rows: list[tuple[Any, ...]] = []
    if external_ids:
        placeholders = ", ".join("?" for _ in external_ids)
        external_rows = conn.execute(
            f"SELECT {external_columns} FROM snapshots s "
            "JOIN externals e ON e.external_id = s.external_id "
            f"WHERE s.snapshot_id IN ({placeholders})",
            tuple(external_ids),
        ).fetchall()

    return note_rows, external_rows

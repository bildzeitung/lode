"""Shared SQL ``IN(...)`` id-batching primitives (lode-oca9, re-cutting lode-r9z0).

Two small, independent seams factored out of duplication across the read paths:

**``placeholders(n)``** -- the ``", ".join("?" for _ in xs)`` idiom, hand-rolled
at ~14 call sites across :mod:`lode.retrieval`, :mod:`lode.notes_read`,
:mod:`lode.worker`, :mod:`lode.enrichment_view` and :mod:`lode.lexical` in three
inconsistent spellings. Takes the *count*, not the sequence, so it composes with
callers that build a SQL string with other params/clauses around the
``IN (...)`` fragment, not just ones that hand a single id list straight to
:func:`fetch_by_ids`. Deliberately **not** used by
:func:`lode.retrieval._in_clause` -- that helper inlines quoted hex literals for
a LanceDB where-predicate where no parameter binding is available, a different
problem with a different (and unsafe outside that one context) answer.

**``fetch_by_ids(conn, ids, sql)``** -- the batched-fetch half of what was
``lode.target_rows.fetch_target_rows`` (lode-r9z0). That helper hardcoded its
own ``FROM ... JOIN ...`` and asked each caller for two SQL column-list
*fragments*, coupled to private table aliases (``v``/``n``/``s``/``e``) named
nowhere but prose -- a rename of either alias would break a caller at runtime,
not at type-check, and the fragments were the one place the helper accepted a
raw, unparameterized SQL string. ``fetch_by_ids`` instead takes the **whole**
SQL string, with one ``{placeholders}`` slot the caller writes and this
function fills, and owns only "skip if empty / build placeholders / bind /
fetchall" -- no hardcoded ``JOIN``, no table-alias contract, and no path that
accepts anything but a fixed, literal SQL string at the call site. The ids
themselves are always bound as ``?`` parameters, never interpolated.

Both are cheap enough to have no independent state or config; importing this
module costs nothing beyond the two functions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any


def placeholders(n: int) -> str:
    """A comma-joined ``"?, ?, ..."`` string of exactly ``n`` placeholders.

    ``n`` is a count, not a sequence -- callers that already have the id
    sequence in hand still pass ``len(ids)`` (or use :func:`fetch_by_ids`,
    which does this for them). Returns ``""`` for ``n == 0``; callers with a
    possibly-empty id list still guard the empty case themselves (an empty
    ``IN ()`` is invalid SQL), the same way every hand-rolled site already
    did.
    """
    return ", ".join("?" for _ in range(n))


def fetch_by_ids(
    conn: sqlite3.Connection, ids: Sequence[str], sql: str
) -> list[tuple[Any, ...]]:
    """Run ``sql`` with its ``{placeholders}`` slot filled and ``ids`` bound, batched.

    ``sql`` is a **fixed, literal string written at the call site** -- e.g.
    ``"SELECT v.version_id, v.body FROM versions v WHERE v.version_id IN
    ({placeholders})"``. It is filled via :meth:`str.format` with exactly one
    keyword, ``placeholders``, computed from ``len(ids)`` by
    :func:`placeholders` above; nothing else about the string is ever
    templated, so there is no path here that turns caller- or user-supplied
    data into an interpolated SQL fragment. ``ids`` are always bound as ``?``
    parameters below, never interpolated -- the placeholder count and the
    bound value count are the same expression (``len(ids)``), so they cannot
    drift out of sync.

    Because :meth:`str.format` owns the whole string, ``sql`` must contain
    **no brace other than the one ``{placeholders}`` slot** -- a caller
    splicing a fragment that carries a stray ``{`` (or a second
    ``{placeholders}``) is a programming error, and every such shape fails
    loudly rather than silently mis-forming a query: a stray ``{`` raises
    ``ValueError``, an unknown ``{name}`` ``KeyError``, a bare ``{}``
    ``IndexError``, and a second ``{placeholders}`` slot doubles the
    placeholder count against an unchanged bound tuple, which SQLite rejects
    with ``ProgrammingError: Incorrect number of bindings supplied``. None of
    these can reach the database as a valid-but-wrong statement.

    Returns ``[]`` without touching the connection when ``ids`` is empty (an
    empty ``IN ()`` is invalid SQL and would also be a wasted round trip) --
    the same short-circuit every hand-rolled site already applied.
    """
    if not ids:
        return []
    return conn.execute(
        sql.format(placeholders=placeholders(len(ids))), tuple(ids)
    ).fetchall()

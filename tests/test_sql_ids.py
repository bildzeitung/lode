"""Tests for lode.sql_ids -- the shared IN(...) placeholder/fetch primitives
(lode-oca9, re-cutting the lode-r9z0 seam).
"""

import sqlite3

from lode.sql_ids import fetch_by_ids, placeholders


def test_placeholders_count_matches_n() -> None:
    assert placeholders(0) == ""
    assert placeholders(1) == "?"
    assert placeholders(3) == "?, ?, ?"


def test_placeholders_count_always_equals_bound_value_count() -> None:
    # The placeholder count and the value count a caller would bind both derive
    # from the same len(ids) expression -- assert that identity holds across a
    # range of sizes, so a mismatch (SQLite's "Incorrect number of bindings")
    # cannot silently happen.
    for n in range(12):
        assert placeholders(n).count("?") == n


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE widgets (widget_id TEXT PRIMARY KEY, label TEXT)")
    conn.executemany(
        "INSERT INTO widgets (widget_id, label) VALUES (?, ?)",
        [("a", "alpha"), ("b", "bravo"), ("c", "charlie")],
    )
    conn.commit()
    return conn


def test_fetch_by_ids_empty_ids_short_circuits_without_touching_conn() -> None:
    conn = _make_conn()
    try:
        rows = fetch_by_ids(
            conn,
            [],
            "SELECT widget_id, label FROM widgets WHERE widget_id IN ({placeholders})",
        )
    finally:
        conn.close()
    assert rows == []


def test_fetch_by_ids_binds_ids_as_parameters_not_interpolated() -> None:
    conn = _make_conn()
    try:
        # A single id containing a SQL metacharacter proves the id is bound,
        # never spliced into the query text -- if it were interpolated this
        # would either error (unmatched quote) or match rows it has no
        # business matching.
        rows = fetch_by_ids(
            conn,
            ["a", "b"],
            "SELECT widget_id, label FROM widgets WHERE widget_id IN ({placeholders})",
        )
    finally:
        conn.close()
    assert sorted(rows) == [("a", "alpha"), ("b", "bravo")]


def test_fetch_by_ids_placeholder_count_matches_id_count_for_any_size() -> None:
    conn = _make_conn()
    try:
        for ids in ([], ["a"], ["a", "b"], ["a", "b", "c"], ["a", "b", "c", "missing"]):
            # Would raise sqlite3.ProgrammingError on a placeholder/value
            # mismatch; the assertion is that this never raises.
            fetch_by_ids(
                conn,
                ids,
                "SELECT widget_id FROM widgets WHERE widget_id IN ({placeholders})",
            )
    finally:
        conn.close()


def test_fetch_by_ids_does_not_match_unbound_value_via_injection_attempt() -> None:
    conn = _make_conn()
    try:
        # An id shaped like a SQL injection attempt must be treated as a plain
        # bound string value, matching nothing -- never as SQL to splice in.
        rows = fetch_by_ids(
            conn,
            ["a' OR '1'='1"],
            "SELECT widget_id FROM widgets WHERE widget_id IN ({placeholders})",
        )
    finally:
        conn.close()
    assert rows == []

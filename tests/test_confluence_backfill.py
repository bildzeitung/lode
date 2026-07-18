"""Tests for lode.confluence_backfill — the Confluence backfill handler (lode-gpzn.11).

Covers the ticket's acceptance criteria: running the backfill for Confluence
on a DB with a pre-existing URL-keyed Confluence tombstone produces a
semantic-key external, re-pointed edges, and a queued refresh that ingests
via the Confluence connector; idempotent on re-run.

Strategy: a real SQLite DB (via init_db), mirroring tests/test_backfill.py's
own end-to-end fake-connector tests -- except here the connector is the real
lode.confluence_backfill.backfill_confluence, not a test-only fake, so this
file also exercises real detection (lode.drawdown._classify_atlassian reuse)
rather than a hand-rolled URL match.
"""

import sqlite3
from pathlib import Path

import pytest

from lode.backfill import registered_backfills, run_backfill
from lode.config import load_settings
from lode.confluence_backfill import backfill_confluence, register
from lode.storage import init_db

_PAGE_URL = "https://acme.atlassian.net/wiki/spaces/ENG/pages/123456789/Design+Doc"
_PAGE_ID = "123456789"
_API_BASE = "https://acme.atlassian.net"


def _confluence_settings(**overrides):
    return load_settings(
        confluence_enabled=True,
        confluence_token="tok",
        confluence_email="a@example.com",
        **overrides,
    )


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the module-level registry across tests -- mirrors
    tests/test_backfill.py's own fixture; register_backfill mutates shared
    module state that must not leak between tests."""
    import lode.backfill as backfill_mod

    saved = dict(backfill_mod._REGISTRY)
    backfill_mod._REGISTRY.clear()
    try:
        yield
    finally:
        backfill_mod._REGISTRY.clear()
        backfill_mod._REGISTRY.update(saved)


def _insert_web_external(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    note_id: str = "note-1",
    quoted_text: str,
    snapshot_status: str | None = None,
) -> None:
    """Seed a source='web' external + a source='user' edge -- the
    "already-processed link" shape (mirrors tests/test_backfill.py's own
    helper). ``quoted_text`` is the literal originally-pasted URL, the piece
    backfill_confluence re-classifies."""
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
            (external_id,),
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, quoted_text, status) "
            "VALUES (?, ?, 'user', ?, 'fresh')",
            (note_id, external_id, quoted_text),
        )
        if snapshot_status is not None:
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES (?, ?, 'body', ?)",
                (f"{external_id}-snap", external_id, snapshot_status),
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
                (f"{external_id}-snap", external_id),
            )


def _job_statuses(conn: sqlite3.Connection, external_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT status FROM jobs WHERE type = 'refresh' AND target_version = ?",
            (external_id,),
        ).fetchall()
    ]


class TestRegister:
    def test_registers_under_confluence_name(self):
        register()
        assert registered_backfills() == ["confluence"]

    def test_idempotent_to_call_more_than_once(self):
        register()
        register()
        assert registered_backfills() == ["confluence"]


class TestBackfillConfluence:
    def test_first_migration_mints_repoints_and_enqueues(
        self, conn: sqlite3.Connection
    ):
        _insert_web_external(conn, _PAGE_URL, note_id="note-1", quoted_text=_PAGE_URL)

        summary = backfill_confluence(
            conn, _confluence_settings(), dry_run=False, retry_tombstoned=False
        )

        assert summary == "migrated 1 link(s)"
        row = conn.execute(
            "SELECT source_type, api_base FROM externals WHERE external_id = ?",
            (_PAGE_ID,),
        ).fetchone()
        assert row == ("confluence", _API_BASE)
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (_PAGE_ID,)
        assert _job_statuses(conn, _PAGE_ID) == ["pending"]

    def test_dry_run_changes_nothing(self, conn: sqlite3.Connection):
        _insert_web_external(conn, _PAGE_URL, note_id="note-1", quoted_text=_PAGE_URL)

        summary = backfill_confluence(
            conn, _confluence_settings(), dry_run=True, retry_tombstoned=False
        )

        assert summary == "migrated 1 link(s)"
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", (_PAGE_ID,)
            ).fetchone()
            is None
        )
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (_PAGE_URL,)  # unchanged
        assert _job_statuses(conn, _PAGE_ID) == []

    def test_ignores_non_atlassian_web_links(self, conn: sqlite3.Connection):
        url = "https://example.com/some/article"
        _insert_web_external(conn, url, note_id="note-1", quoted_text=url)

        summary = backfill_confluence(
            conn, _confluence_settings(), dry_run=False, retry_tombstoned=False
        )

        assert summary == "migrated 0 link(s)"
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (url,)  # untouched

    def test_ignores_jira_links(self, conn: sqlite3.Connection):
        # A matched Atlassian host but the JIRA shape, not Confluence's. JIRA
        # is flagged on *too* here so _classify_atlassian genuinely returns a
        # SOURCE_TYPE_JIRA classification -- exercising the handler's real
        # classified[0] != SOURCE_TYPE_CONFLUENCE discrimination branch, not
        # merely the "connector inactive -> None" path. This handler must not
        # touch a JIRA link (that's lode-gpzn.10's own handler).
        url = "https://acme.atlassian.net/browse/ABC-123"
        _insert_web_external(conn, url, note_id="note-1", quoted_text=url)

        settings = _confluence_settings(
            jira_enabled=True, jira_token="tok", jira_email="a@example.com"
        )
        summary = backfill_confluence(
            conn, settings, dry_run=False, retry_tombstoned=False
        )

        assert summary == "migrated 0 link(s)"
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (url,)

    def test_flag_off_finds_nothing(self, conn: sqlite3.Connection):
        # Confluence not flagged on / no credentials -- current routing means
        # no Atlassian match is attempted (mirrors detect_and_enqueue_
        # drawdown's own flag-off behavior); the link is left untouched.
        _insert_web_external(conn, _PAGE_URL, note_id="note-1", quoted_text=_PAGE_URL)

        summary = backfill_confluence(
            conn, load_settings(), dry_run=False, retry_tombstoned=False
        )

        assert summary == "migrated 0 link(s)"
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", (_PAGE_ID,)
            ).fetchone()
            is None
        )

    def test_ignores_ai_sourced_edges(self, conn: sqlite3.Connection):
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
                (_PAGE_URL,),
            )
            conn.execute(
                "INSERT INTO edges (from_id, to_id, source, quoted_text, status) "
                "VALUES ('note-1', ?, 'ai', ?, 'fresh')",
                (_PAGE_URL, _PAGE_URL),
            )

        summary = backfill_confluence(
            conn, _confluence_settings(), dry_run=False, retry_tombstoned=False
        )

        assert summary == "migrated 0 link(s)"

    def test_second_run_is_idempotent(self, conn: sqlite3.Connection):
        _insert_web_external(conn, _PAGE_URL, note_id="note-1", quoted_text=_PAGE_URL)
        settings = _confluence_settings()

        first = backfill_confluence(
            conn, settings, dry_run=False, retry_tombstoned=False
        )
        second = backfill_confluence(
            conn, settings, dry_run=False, retry_tombstoned=False
        )

        assert first == "migrated 1 link(s)"
        # The edge now points at the confluence-typed external (no longer
        # 'web'), so the second pass finds nothing new to migrate -- the
        # ticket's own "idempotent on re-run" acceptance criterion.
        assert second == "migrated 0 link(s)"
        assert _job_statuses(conn, _PAGE_ID) == ["pending"]
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (_PAGE_ID,)

    def test_multiple_links_all_migrated(self, conn: sqlite3.Connection):
        url_a = "https://acme.atlassian.net/wiki/spaces/ENG/pages/111/A"
        url_b = "https://acme.atlassian.net/wiki/spaces/ENG/pages/222/B"
        _insert_web_external(conn, url_a, note_id="note-1", quoted_text=url_a)
        _insert_web_external(conn, url_b, note_id="note-2", quoted_text=url_b)

        summary = backfill_confluence(
            conn, _confluence_settings(), dry_run=False, retry_tombstoned=False
        )

        assert summary == "migrated 2 link(s)"
        assert _job_statuses(conn, "111") == ["pending"]
        assert _job_statuses(conn, "222") == ["pending"]


class TestRunBackfillIntegration:
    def test_run_backfill_dispatches_through_the_framework(
        self, conn: sqlite3.Connection
    ):
        register()
        _insert_web_external(conn, _PAGE_URL, note_id="note-1", quoted_text=_PAGE_URL)

        summary = run_backfill(conn, _confluence_settings(), "confluence")

        assert summary == "migrated 1 link(s)"

    def test_run_backfill_threads_dry_run(self, conn: sqlite3.Connection):
        register()
        _insert_web_external(conn, _PAGE_URL, note_id="note-1", quoted_text=_PAGE_URL)

        summary = run_backfill(conn, _confluence_settings(), "confluence", dry_run=True)

        assert summary == "migrated 1 link(s)"
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", (_PAGE_ID,)
            ).fetchone()
            is None
        )

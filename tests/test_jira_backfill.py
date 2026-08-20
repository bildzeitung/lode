"""Tests for lode.jira_backfill -- the JIRA connector's backfill handler
(lode-gpzn.10).

Strategy: exercise ``_jira_backfill`` directly against a real SQLite DB (via
``init_db``), mirroring tests/test_backfill.py's own strategy and helper
style. A JIRA-active ``Settings`` (flag on + resolvable credentials) is
required for every "would migrate" case, since ``_jira_backfill`` reclassifies
through ``lode.drawdown._classify_atlassian`` -- the exact same gate live
paste-time draw-down applies (``lode.config.jira_active``).
"""

import sqlite3
from pathlib import Path

import pytest

from lode.backfill import needs_refresh, registered_backfills
from lode.config import load_settings
from lode.jira_backfill import _jira_backfill, register
from lode.storage import init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jira_settings(**overrides):
    return load_settings(
        jira_enabled=True, jira_token="tok", jira_email="a@example.com", **overrides
    )


def _insert_web_external(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    note_id: str = "note-1",
    quoted_text: str | None = None,
) -> None:
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


def _insert_snapshot(
    conn: sqlite3.Connection,
    external_id: str,
    snapshot_id: str,
    *,
    status: str = "ok",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
            "VALUES (?, ?, 'body', ?)",
            (snapshot_id, external_id, status),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )


def _job_statuses(conn: sqlite3.Connection, external_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT status FROM jobs WHERE type = 'refresh' AND target_version = ?",
            (external_id,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registers_under_jira_name(self):
        register()
        assert registered_backfills() == ["jira"]

    def test_idempotent_to_call_more_than_once(self):
        register()
        register()
        assert registered_backfills() == ["jira"]


# ---------------------------------------------------------------------------
# _jira_backfill
# ---------------------------------------------------------------------------


class TestJiraBackfill:
    def test_first_migration_mints_repoints_and_enqueues(
        self, conn: sqlite3.Connection
    ):
        url = "https://acme.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, quoted_text=url)

        summary = _jira_backfill(conn, _jira_settings(), False, False)

        assert "migrated 1" in summary
        row = conn.execute(
            "SELECT source_type, api_base FROM externals WHERE external_id = 'ABC-1'"
        ).fetchone()
        assert row == ("jira", "https://acme.atlassian.net")
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == ("ABC-1",)
        assert _job_statuses(conn, "ABC-1") == ["pending"]

    def test_dry_run_changes_nothing(self, conn: sqlite3.Connection):
        url = "https://acme.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, quoted_text=url)

        summary = _jira_backfill(conn, _jira_settings(), True, False)

        assert "migrated 1" in summary
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = 'ABC-1'"
            ).fetchone()
            is None
        )
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (url,)  # unrepointed
        assert _job_statuses(conn, "ABC-1") == []

    def test_idempotent_on_rerun_no_double_mint_or_repoint(
        self, conn: sqlite3.Connection
    ):
        url = "https://acme.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, quoted_text=url)
        settings = _jira_settings()

        _jira_backfill(conn, settings, False, False)
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'done' WHERE type = 'refresh' "
                "AND target_version = 'ABC-1'"
            )
        _insert_snapshot(conn, "ABC-1", "snap-1", status="ok")

        summary = _jira_backfill(conn, settings, False, False)

        # Nothing left to migrate -- the edge already points at the
        # semantic key -- but a fresh refresh IS enqueued again (head
        # snapshot isn't a tombstone, so needs_refresh is still True).
        assert "migrated 0" in summary
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM externals WHERE external_id = 'ABC-1'"
            ).fetchone()[0]
            == 1
        )
        assert sorted(_job_statuses(conn, "ABC-1")) == ["done", "pending"]

    def test_rerun_over_tombstoned_target_needs_override(
        self, conn: sqlite3.Connection
    ):
        url = "https://acme.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, quoted_text=url)
        settings = _jira_settings()

        _jira_backfill(conn, settings, False, False)
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'dead' WHERE type = 'refresh' "
                "AND target_version = 'ABC-1'"
            )
        _insert_snapshot(conn, "ABC-1", "snap-1", status="tombstone")
        assert needs_refresh(conn, "ABC-1") is False  # sanity, mirrors gpzn.9

        # Without the override: excluded, no new job.
        summary = _jira_backfill(conn, settings, False, False)
        assert "enqueued 0 refresh" in summary
        assert sorted(_job_statuses(conn, "ABC-1")) == ["dead"]

        # With the override: the one case it's load-bearing for.
        summary = _jira_backfill(conn, settings, False, True)
        assert "enqueued 1 refresh" in summary
        assert sorted(_job_statuses(conn, "ABC-1")) == ["dead", "pending"]

    def test_flag_off_leaves_link_untouched(self, conn: sqlite3.Connection):
        url = "https://acme.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, quoted_text=url)

        summary = _jira_backfill(conn, load_settings(), False, False)

        assert "migrated 0" in summary
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = 'ABC-1'"
            ).fetchone()
            is None
        )
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == (url,)

    def test_non_atlassian_link_ignored(self, conn: sqlite3.Connection):
        url = "https://example.com/a"
        _insert_web_external(conn, url, quoted_text=url)

        summary = _jira_backfill(conn, _jira_settings(), False, False)

        assert "migrated 0" in summary

    def test_confluence_link_ignored_by_jira_handler(self, conn: sqlite3.Connection):
        url = "https://acme.atlassian.net/wiki/spaces/SPACE/pages/123/Title"
        _insert_web_external(conn, url, quoted_text=url)
        settings = load_settings(
            jira_enabled=True,
            jira_token="tok",
            jira_email="a@example.com",
            confluence_enabled=True,
            confluence_token="tok",
            confluence_email="a@example.com",
        )

        summary = _jira_backfill(conn, settings, False, False)

        assert "migrated 0" in summary
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE source_type = 'jira'"
            ).fetchone()
            is None
        )

    def test_edge_with_no_quoted_text_skipped(self, conn: sqlite3.Connection):
        _insert_web_external(
            conn,
            "https://acme.atlassian.net/browse/ABC-1",
            quoted_text=None,
        )

        summary = _jira_backfill(conn, _jira_settings(), False, False)

        assert "migrated 0" in summary

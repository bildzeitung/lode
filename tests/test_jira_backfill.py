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


def _insert_live_note(
    conn: sqlite3.Connection, note_id: str, version_id: str, body: str
) -> None:
    """A minimal live note head -- no drawdown call, so no edge is created.

    Mirrors exactly what a note saved *before* the bare-key feature (or
    before its project prefix was allow-listed) would look like: a live
    head version whose body may contain a bare key, with no
    ``source='user'`` edge for it at all.
    """
    with conn:
        # notes.head_version_id -> versions is DEFERRABLE (schema.sql) --
        # insert the note first (head_version_id NULL), then the version
        # (its own FK to note_id needs the note row to already exist), then
        # point the note's head at it, exactly the order Repository.save
        # itself uses for a note's first version.
        conn.execute("INSERT INTO notes (note_id) VALUES (?)", (note_id,))
        conn.execute(
            "INSERT INTO versions (version_id, note_id, parent_version_id, body, op) "
            "VALUES (?, ?, NULL, ?, 'create')",
            (version_id, note_id, body),
        )
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
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


# ---------------------------------------------------------------------------
# Bare JIRA issue keys (lode-2o45)
# ---------------------------------------------------------------------------


def _bare_key_settings(**overrides):
    return _jira_settings(
        jira_projects=["PROJ"],
        jira_base_url="https://acme.atlassian.net",
        **overrides,
    )


class TestBareJiraKeyBackfill:
    def test_predating_bare_key_is_minted_and_enqueued(self, conn: sqlite3.Connection):
        """A note containing PROJ-42, saved before jira_projects listed PROJ:
        lode backfill mints the external and enqueues a refresh."""
        _insert_live_note(conn, "note-1", "ver-1", "see PROJ-42 for context")

        summary = _jira_backfill(conn, _bare_key_settings(), False, False)

        assert "migrated 1" in summary
        row = conn.execute(
            "SELECT source_type, api_base FROM externals WHERE external_id = 'PROJ-42'"
        ).fetchone()
        assert row == ("jira", "https://acme.atlassian.net")
        edge = conn.execute(
            "SELECT to_id, quoted_text FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == ("PROJ-42", "PROJ-42")
        assert _job_statuses(conn, "PROJ-42") == ["pending"]

    def test_second_pass_mints_and_links_nothing_new(
        self, conn: sqlite3.Connection
    ) -> None:
        """No new externals row, no new edge, and no duplicate job row on a
        second pass -- the one-edge-per-note-per-key idempotency the bare-key
        scan itself guarantees. A fresh `refresh` job IS still reported
        enqueued each pass (mirrors the URL loop's own re-run behavior,
        `test_idempotent_on_rerun_no_double_mint_or_repoint` above) -- the
        live-job dedup index (`idx_jobs_live`) is what actually keeps that a
        no-op at the row level, not the backfill's own bookkeeping."""
        _insert_live_note(conn, "note-1", "ver-1", "see PROJ-42 for context")
        settings = _bare_key_settings()

        _jira_backfill(conn, settings, False, False)
        summary = _jira_backfill(conn, settings, False, False)

        assert "migrated 0" in summary
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert n == 1
        (externals_n,) = conn.execute(
            "SELECT COUNT(*) FROM externals WHERE external_id = 'PROJ-42'"
        ).fetchone()
        assert externals_n == 1
        assert _job_statuses(conn, "PROJ-42") == ["pending"]  # no duplicate row

    def test_key_inside_a_url_span_is_not_double_linked(self, conn: sqlite3.Connection):
        url = "https://acme.atlassian.net/browse/PROJ-42"
        body = f"see {url} for context"
        _insert_web_external(conn, url, quoted_text=url)  # the URL loop's own state
        _insert_live_note(conn, "note-1", "ver-1", body)

        summary = _jira_backfill(conn, _bare_key_settings(), False, False)

        # The URL loop above already migrates/mints this -- the bare-key
        # scan must not also link it a second time from the same note.
        assert "migrated 1" in summary
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert n == 1

    def test_jira_projects_empty_scans_nothing(self, conn: sqlite3.Connection):
        _insert_live_note(conn, "note-1", "ver-1", "see PROJ-42 for context")

        summary = _jira_backfill(conn, _jira_settings(), False, False)

        assert "migrated 0" in summary
        assert (
            conn.execute("SELECT 1 FROM edges WHERE from_id = 'note-1'").fetchone()
            is None
        )

    def test_base_url_empty_scans_nothing(self, conn: sqlite3.Connection):
        _insert_live_note(conn, "note-1", "ver-1", "see PROJ-42 for context")
        settings = _jira_settings(jira_projects=["PROJ"])  # no jira_base_url

        summary = _jira_backfill(conn, settings, False, False)

        assert "migrated 0" in summary
        assert (
            conn.execute("SELECT 1 FROM edges WHERE from_id = 'note-1'").fetchone()
            is None
        )

    def test_dry_run_scans_but_writes_nothing(self, conn: sqlite3.Connection):
        _insert_live_note(conn, "note-1", "ver-1", "see PROJ-42 for context")

        summary = _jira_backfill(conn, _bare_key_settings(), True, False)

        assert "migrated 1" in summary
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = 'PROJ-42'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute("SELECT 1 FROM edges WHERE from_id = 'note-1'").fetchone()
            is None
        )

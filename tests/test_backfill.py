"""Tests for lode.backfill — the per-connector backfill framework (lode-gpzn.9).

Covers the acceptance criteria (bd show lode-gpzn.9): a connector can register
a backfill handler (registry seam); the shared iterate/repoint/enqueue
plumbing is reused rather than reimplemented; --dry-run reports without
writing; and the tombstone-exclusion override is exercised only on a re-run
over an already-tombstoned semantic external, never on first migration.

Strategy: a real SQLite DB (via init_db) so the plumbing functions' actual
SQL (and jobs.py's idx_jobs_live dedup) is exercised, mirroring
tests/test_reconcile.py and tests/test_drawdown.py. edges/jobs/externals rows
are seeded directly with SQL (no FK to notes on edges/jobs), same technique
tests/test_reconcile.py's _insert_external_snapshot uses.
"""

import sqlite3
from pathlib import Path

import pytest

from lode.backfill import (
    BackfillError,
    LinkedExternal,
    enqueue_fresh_refresh,
    iter_user_linked_externals,
    mint_external,
    needs_refresh,
    register_backfill,
    registered_backfills,
    repoint_edges,
    run_backfill,
)
from lode.config import Settings
from lode.externals import set_no_egress
from lode.storage import init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _no_egress(conn: sqlite3.Connection, external_id: str) -> int:
    (no_egress,) = conn.execute(
        "SELECT no_egress FROM externals WHERE external_id = ?", (external_id,)
    ).fetchone()
    return no_egress


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the module-level registry across tests (mirrors reconcile's
    _STEPS being test-managed) -- register_backfill mutates shared state."""
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


def _insert_web_external(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    note_id: str = "note-1",
    quoted_text: str | None = None,
) -> None:
    """Seed a source='web' external plus a source='user' edge linking it --
    the "already-processed link" shape backfill iterates over. No FK from
    edges/externals to notes, so note_id is a bare string (mirrors
    tests/test_reconcile.py's direct-SQL seeding style)."""
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
            (external_id,),
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, quoted_text, status) "
            "VALUES (?, ?, 'user', ?, 'fresh')",
            (note_id, external_id, quoted_text or external_id),
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
# Registry: register_backfill / registered_backfills / run_backfill
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registered_backfills_starts_empty(self):
        assert registered_backfills() == []

    def test_register_backfill_makes_it_discoverable(self):
        register_backfill("jira", lambda conn, settings, dry_run, retry: "ok")
        assert registered_backfills() == ["jira"]

    def test_registered_backfills_sorted(self):
        register_backfill("zeta", lambda *a: "z")
        register_backfill("alpha", lambda *a: "a")
        assert registered_backfills() == ["alpha", "zeta"]

    def test_run_backfill_dispatches_to_registered_handler(
        self, conn: sqlite3.Connection
    ):
        calls = []

        def handler(c, settings, dry_run, retry_tombstoned):
            calls.append((c is conn, dry_run, retry_tombstoned))
            return "did the thing"

        register_backfill("jira", handler)
        result = run_backfill(
            conn, Settings(), "jira", dry_run=True, retry_tombstoned=True
        )
        assert result == "did the thing"
        assert calls == [(True, True, True)]

    def test_run_backfill_defaults_dry_run_and_retry_false(
        self, conn: sqlite3.Connection
    ):
        seen = {}

        def handler(c, settings, dry_run, retry_tombstoned):
            seen["dry_run"] = dry_run
            seen["retry_tombstoned"] = retry_tombstoned
            return "ok"

        register_backfill("jira", handler)
        run_backfill(conn, Settings(), "jira")
        assert seen == {"dry_run": False, "retry_tombstoned": False}

    def test_run_backfill_unknown_connector_raises_backfill_error(
        self, conn: sqlite3.Connection
    ):
        register_backfill("jira", lambda *a: "ok")
        with pytest.raises(BackfillError) as exc_info:
            run_backfill(conn, Settings(), "confluence")
        message = str(exc_info.value)
        assert "confluence" in message
        assert "jira" in message  # names the available connector(s)

    def test_run_backfill_unknown_connector_no_registrants(
        self, conn: sqlite3.Connection
    ):
        with pytest.raises(BackfillError) as exc_info:
            run_backfill(conn, Settings(), "jira")
        assert "none registered" in str(exc_info.value)


# ---------------------------------------------------------------------------
# iter_user_linked_externals
# ---------------------------------------------------------------------------


class TestIterUserLinkedExternals:
    def test_yields_nothing_when_no_edges(self, conn: sqlite3.Connection):
        assert list(iter_user_linked_externals(conn)) == []

    def test_yields_source_user_edge(self, conn: sqlite3.Connection):
        _insert_web_external(
            conn,
            "https://example.atlassian.net/browse/ABC-1",
            note_id="note-1",
            quoted_text="https://example.atlassian.net/browse/ABC-1",
        )
        items = list(iter_user_linked_externals(conn))
        assert items == [
            LinkedExternal(
                note_id="note-1",
                external_id="https://example.atlassian.net/browse/ABC-1",
                source_type="web",
                quoted_text="https://example.atlassian.net/browse/ABC-1",
            )
        ]

    def test_excludes_source_ai_edges(self, conn: sqlite3.Connection):
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
                ("https://example.com/a",),
            )
            conn.execute(
                "INSERT INTO edges (from_id, to_id, source, status) "
                "VALUES ('note-1', 'https://example.com/a', 'ai', 'fresh')"
            )
        assert list(iter_user_linked_externals(conn)) == []

    def test_multiple_edges_ordered(self, conn: sqlite3.Connection):
        _insert_web_external(conn, "https://example.com/b", note_id="note-2")
        _insert_web_external(conn, "https://example.com/a", note_id="note-1")
        items = list(iter_user_linked_externals(conn))
        assert [(i.note_id, i.external_id) for i in items] == [
            ("note-1", "https://example.com/a"),
            ("note-2", "https://example.com/b"),
        ]


# ---------------------------------------------------------------------------
# mint_external
# ---------------------------------------------------------------------------


class TestMintExternal:
    def test_inserts_fresh_row(self, conn: sqlite3.Connection):
        created = mint_external(conn, "ABC-1", "jira", "https://example.atlassian.net")
        assert created is True
        row = conn.execute(
            "SELECT source_type, api_base FROM externals WHERE external_id = ?",
            ("ABC-1",),
        ).fetchone()
        assert row == ("jira", "https://example.atlassian.net")

    def test_second_mint_is_a_noop(self, conn: sqlite3.Connection):
        mint_external(conn, "ABC-1", "jira")
        created_again = mint_external(conn, "ABC-1", "jira")
        assert created_again is False

    def test_dry_run_writes_nothing(self, conn: sqlite3.Connection):
        would_create = mint_external(conn, "ABC-1", "jira", dry_run=True)
        assert would_create is True
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = 'ABC-1'"
            ).fetchone()
            is None
        )

    def test_dry_run_reports_false_when_already_exists(self, conn: sqlite3.Connection):
        mint_external(conn, "ABC-1", "jira")
        assert mint_external(conn, "ABC-1", "jira", dry_run=True) is False

    def test_defaults_no_egress_false(self, conn: sqlite3.Connection):
        """The ordinary (default) case: a freshly minted external is cloud-eligible."""
        mint_external(conn, "ABC-1", "jira")
        assert _no_egress(conn, "ABC-1") == 0

    def test_omitted_settings_logs_a_warning(
        self, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """lode-xa5d: mint_external is privacy-bearing (seeds no_egress), so an
        omitted ``settings=`` must be loud, not a silent library-defaults fallback.
        """
        with caplog.at_level("WARNING", logger="lode.config"):
            mint_external(conn, "ABC-1", "jira")
        assert any("backfill.mint_external" in r.getMessage() for r in caplog.records)

    def test_honors_settings_no_egress_default(self, conn: sqlite3.Connection):
        """Settings.no_egress_default=True must apply at the row's true first
        insert (lode-ge8w) -- previously only the schema DEFAULT 0 was
        consulted, silently leaving every backfill-minted external
        cloud-eligible.
        """
        mint_external(conn, "ABC-1", "jira", settings=Settings(no_egress_default=True))
        assert _no_egress(conn, "ABC-1") == 1

    def test_default_does_not_stomp_an_explicit_clear_on_a_noop_remint(
        self, conn: sqlite3.Connection
    ):
        """ON CONFLICT (external_id) DO NOTHING must never re-apply the
        default over a user's explicit 'lode no-egress --clear' on a later
        no-op re-mint of the SAME external_id.
        """
        settings = Settings(no_egress_default=True)
        mint_external(conn, "ABC-1", "jira", settings=settings)
        # The real clear path, not a hand-rolled UPDATE: this is what
        # 'lode no-egress --clear' actually executes.
        set_no_egress(conn, "ABC-1", no_egress=False)

        created_again = mint_external(conn, "ABC-1", "jira", settings=settings)

        assert created_again is False
        assert _no_egress(conn, "ABC-1") == 0


# ---------------------------------------------------------------------------
# repoint_edges
# ---------------------------------------------------------------------------


class TestRepointEdges:
    def test_repoints_user_edges(self, conn: sqlite3.Connection):
        _insert_web_external(conn, "https://example.atlassian.net/browse/ABC-1")
        mint_external(conn, "ABC-1", "jira")
        count = repoint_edges(
            conn, "https://example.atlassian.net/browse/ABC-1", "ABC-1"
        )
        assert count == 1
        row = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert row == ("ABC-1",)

    def test_never_touches_ai_edges(self, conn: sqlite3.Connection):
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
                ("https://old.example.com/x",),
            )
            conn.execute(
                "INSERT INTO edges (from_id, to_id, source, status) "
                "VALUES ('note-1', 'https://old.example.com/x', 'ai', 'fresh')"
            )
        repoint_edges(conn, "https://old.example.com/x", "NEW-1")
        row = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert row == ("https://old.example.com/x",)

    def test_dry_run_reports_count_without_writing(self, conn: sqlite3.Connection):
        _insert_web_external(conn, "https://example.atlassian.net/browse/ABC-1")
        count = repoint_edges(
            conn,
            "https://example.atlassian.net/browse/ABC-1",
            "ABC-1",
            dry_run=True,
        )
        assert count == 1
        row = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert row == ("https://example.atlassian.net/browse/ABC-1",)  # unchanged

    def test_dry_run_zero_when_no_matching_edges(self, conn: sqlite3.Connection):
        assert (
            repoint_edges(conn, "https://nowhere.example.com", "X", dry_run=True) == 0
        )


# ---------------------------------------------------------------------------
# needs_refresh — the tombstone-exclusion override
# ---------------------------------------------------------------------------


class TestNeedsRefresh:
    def test_true_when_no_external_row_yet(self, conn: sqlite3.Connection):
        # First migration: the target hasn't been minted yet at all.
        assert needs_refresh(conn, "ABC-1") is True
        assert needs_refresh(conn, "ABC-1", retry_tombstoned=True) is True

    def test_true_when_external_has_no_head_snapshot(self, conn: sqlite3.Connection):
        # First migration: mint_external ran, but nothing fetched yet.
        mint_external(conn, "ABC-1", "jira")
        assert needs_refresh(conn, "ABC-1") is True

    def test_true_when_head_snapshot_ok(self, conn: sqlite3.Connection):
        mint_external(conn, "ABC-1", "jira")
        _insert_snapshot(conn, "ABC-1", "snap-1", status="ok")
        assert needs_refresh(conn, "ABC-1") is True

    def test_false_when_head_tombstone_and_no_override(self, conn: sqlite3.Connection):
        # Re-run case, override NOT requested: excluded, mirroring
        # reconcile.py's own tombstone exclusion.
        mint_external(conn, "ABC-1", "jira")
        _insert_snapshot(conn, "ABC-1", "snap-1", status="tombstone")
        assert needs_refresh(conn, "ABC-1") is False
        assert needs_refresh(conn, "ABC-1", retry_tombstoned=False) is False

    def test_true_when_head_tombstone_and_override_requested(
        self, conn: sqlite3.Connection
    ):
        # Re-run case, override requested: this is the ONE case the override
        # is load-bearing for (ticket acceptance).
        mint_external(conn, "ABC-1", "jira")
        _insert_snapshot(conn, "ABC-1", "snap-1", status="tombstone")
        assert needs_refresh(conn, "ABC-1", retry_tombstoned=True) is True


# ---------------------------------------------------------------------------
# enqueue_fresh_refresh
# ---------------------------------------------------------------------------


class TestEnqueueFreshRefresh:
    def test_enqueues_a_pending_refresh_job(self, conn: sqlite3.Connection):
        enqueue_fresh_refresh(conn, "ABC-1")
        assert _job_statuses(conn, "ABC-1") == ["pending"]

    def test_dry_run_enqueues_nothing(self, conn: sqlite3.Connection):
        enqueue_fresh_refresh(conn, "ABC-1", dry_run=True)
        assert _job_statuses(conn, "ABC-1") == []

    def test_idempotent_against_a_live_job(self, conn: sqlite3.Connection):
        enqueue_fresh_refresh(conn, "ABC-1")
        enqueue_fresh_refresh(conn, "ABC-1")
        assert _job_statuses(conn, "ABC-1") == ["pending"]

    def test_reenqueues_after_prior_job_done(self, conn: sqlite3.Connection):
        enqueue_fresh_refresh(conn, "ABC-1")
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'done' WHERE type = 'refresh' "
                "AND target_version = 'ABC-1'"
            )
        enqueue_fresh_refresh(conn, "ABC-1")
        assert sorted(_job_statuses(conn, "ABC-1")) == ["done", "pending"]


# ---------------------------------------------------------------------------
# End-to-end: a fake connector handler composed entirely from the framework's
# shared plumbing -- exercises first-migration vs. re-run-with-override as a
# whole flow, the way lode-gpzn.10/.11's real handlers eventually will.
# ---------------------------------------------------------------------------


def _fake_atlassian_backfill(conn, settings, dry_run, retry_tombstoned):
    """Re-classify every web-typed source='user' edge whose quoted_text looks
    like an Atlassian issue link, mint a fresh jira-typed external, repoint
    the edge, and enqueue a fresh refresh -- composed entirely from
    lode.backfill's own shared plumbing (no hand-rolled SQL of its own)."""
    migrated = 0
    for link in iter_user_linked_externals(conn):
        if link.source_type != "web" or "/browse/" not in (link.quoted_text or ""):
            continue
        key = link.quoted_text.rsplit("/browse/", 1)[1]
        mint_external(conn, key, "jira", dry_run=dry_run)
        repoint_edges(conn, link.external_id, key, dry_run=dry_run)
        if needs_refresh(conn, key, retry_tombstoned=retry_tombstoned):
            enqueue_fresh_refresh(conn, key, dry_run=dry_run)
            migrated += 1
    return f"migrated {migrated} link(s)"


class TestEndToEndFakeConnector:
    def test_first_migration_mints_repoints_and_enqueues(
        self, conn: sqlite3.Connection
    ):
        url = "https://example.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, note_id="note-1", quoted_text=url)
        register_backfill("jira", _fake_atlassian_backfill)

        summary = run_backfill(conn, Settings(), "jira")

        assert summary == "migrated 1 link(s)"
        # externals row minted
        row = conn.execute(
            "SELECT source_type FROM externals WHERE external_id = 'ABC-1'"
        ).fetchone()
        assert row == ("jira",)
        # edge repointed
        edge = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = 'note-1'"
        ).fetchone()
        assert edge == ("ABC-1",)
        # refresh enqueued
        assert _job_statuses(conn, "ABC-1") == ["pending"]

    def test_dry_run_changes_nothing(self, conn: sqlite3.Connection):
        url = "https://example.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, note_id="note-1", quoted_text=url)
        register_backfill("jira", _fake_atlassian_backfill)

        summary = run_backfill(conn, Settings(), "jira", dry_run=True)

        assert summary == "migrated 1 link(s)"
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
        assert _job_statuses(conn, "ABC-1") == []

    def test_rerun_over_tombstoned_target_needs_override(
        self, conn: sqlite3.Connection
    ):
        url = "https://example.atlassian.net/browse/ABC-1"
        _insert_web_external(conn, url, note_id="note-1", quoted_text=url)
        register_backfill("jira", _fake_atlassian_backfill)

        # First pass: mints + repoints + enqueues.
        run_backfill(conn, Settings(), "jira")
        # Simulate the enqueued refresh permanently failing (bad token -> 401
        # -> tombstone), exactly as the ticket's own decision-D scenario.
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'dead' WHERE type = 'refresh' "
                "AND target_version = 'ABC-1'"
            )
        _insert_snapshot(conn, "ABC-1", "snap-1", status="tombstone")

        # Re-run WITHOUT the override: the edge is already repointed (so the
        # handler's own filter finds nothing new to migrate under source_type
        # == 'web' -- but confirm needs_refresh itself still gates a direct
        # re-enqueue attempt against the tombstoned target).
        assert needs_refresh(conn, "ABC-1") is False

        # Re-run WITH the override: exercised only here, on the re-run over
        # an already-tombstoned target -- the ticket's acceptance criterion.
        assert needs_refresh(conn, "ABC-1", retry_tombstoned=True) is True
        enqueue_fresh_refresh(conn, "ABC-1")
        assert sorted(_job_statuses(conn, "ABC-1")) == ["dead", "pending"]

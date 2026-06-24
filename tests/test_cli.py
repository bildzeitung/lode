"""Tests for the lode CLI.

Covers the skeleton surface (lode-txh.5: the subcommands exist, dispatch, and are
listed by ``--help``), the real ``lode add`` capture command (lode-y42.1) — it
persists via ``versions.save`` with no AI in the path, enqueues embed/enrich
derive jobs, refuses an empty note, and on a CAS reject preserves the buffer as a
draft rather than clobbering — the operational read-outs (lode-y42.3): ``status``
(job-queue health, dead-letters, egress summary), ``jobs`` (list/filter the derive
queue); the ``egress`` audit read-out (lode-fk8.3: per-send ts/purpose/model/sent
ids/redactions); and ``purge`` (the E8 hard delete via ``Repository.purge``,
lode-7cx).
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lode import __version__, cli
from lode.cli import app
from lode.hashing import NO_PARENT, content_version_id
from lode.storage import init_db
from lode.versions import save

runner = CliRunner()

# `add` (lode-y42.1), `status` / `jobs` (lode-y42.3) and `purge` (lode-7cx) are
# real; `ask` / `eval` are still dispatching stubs.
STUB_SUBCOMMANDS = ["ask", "eval"]
ALL_SUBCOMMANDS = ["add", "ask", "purge", "status", "jobs", "egress", "eval"]


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_usage() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ALL_SUBCOMMANDS:
        assert name in result.stdout


@pytest.mark.parametrize("name", STUB_SUBCOMMANDS)
def test_subcommand_dispatches(name: str) -> None:
    result = runner.invoke(app, [name])
    assert result.exit_code == 0
    assert name in result.stdout


# --- lode add ---------------------------------------------------------------


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_add_captures_note_and_enqueues_derive_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "hello world", "--db", str(db_path)])
    assert result.exit_code == 0
    note_id = result.stdout.strip()

    # The note and its root version are persisted via versions.save.
    assert _rows(
        db_path, "SELECT note_id, body, op FROM versions WHERE note_id = ?", (note_id,)
    ) == [(note_id, "hello world", "create")]

    # Exactly the embed + enrich derive jobs, pending, targeting the new version.
    (version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    )[0]
    assert _rows(
        db_path,
        "SELECT type, status, prompt_ver FROM jobs WHERE target_version = ? "
        "ORDER BY type",
        (version_id,),
    ) == [("embed", "pending", None), ("enrich", "pending", None)]


def test_add_reads_body_from_stdin_verbatim(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "--db", str(db_path)], input="from stdin\n")
    assert result.exit_code == 0
    note_id = result.stdout.strip()
    # Stored verbatim — the trailing newline is preserved, not stripped.
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (note_id,)
    ) == [("from stdin\n",)]


def test_add_uses_lode_db_env_var(tmp_path: Path) -> None:
    db_path = tmp_path / "env.db"
    result = runner.invoke(app, ["add", "via env"], env={"LODE_DB": str(db_path)})
    assert result.exit_code == 0
    assert db_path.exists()


@pytest.mark.parametrize("body", ["", "   ", "\n\t  \n"])
def test_add_refuses_empty_or_whitespace_note(tmp_path: Path, body: str) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", body, "--db", str(db_path)])
    assert result.exit_code == 1
    # Nothing persisted: not even the DB file is left behind.
    assert not db_path.exists()


def test_add_creates_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "lode.db"
    result = runner.invoke(app, ["add", "deep", "--db", str(db_path)])
    assert result.exit_code == 0
    assert db_path.exists()


class _FixedUUID:
    """Stand-in so ``str(uuid4())`` yields a chosen note id (forces a collision)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def test_add_cas_reject_writes_draft_and_does_not_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    fixed_id = "fixed-note-id"
    # Pre-create the note so the minted-id create collides -> HeadConflictError.
    conn = init_db(db_path)
    try:
        save(conn, fixed_id, "original body")
    finally:
        conn.close()
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: _FixedUUID(fixed_id))

    result = runner.invoke(app, ["add", "rejected body", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "note changed since opened" in result.stderr

    # The original note is untouched (no clobber, no auto-merge).
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (fixed_id,)
    ) == [("original body",)]
    (head,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (fixed_id,)
    )[0]
    assert head == content_version_id(fixed_id, NO_PARENT, "original body")

    # The rejected buffer is preserved as a draft beside the DB.
    drafts = list(db_path.parent.glob(f"{fixed_id}.*.draft"))
    assert len(drafts) == 1
    assert drafts[0].read_text(encoding="utf-8") == "rejected body"


# --- lode status / jobs (lode-y42.3) ----------------------------------------


def _seed_jobs(db_path: Path) -> None:
    """Seed a spread of job rows + egress_log rows to read back via status/jobs."""
    conn = init_db(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO jobs (type, target_version, status, attempts, last_error) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("embed", "ver-aaaaaaaaaaaaaaaa", "pending", 0, None),
                    ("enrich", "ver-aaaaaaaaaaaaaaaa", "running", 1, None),
                    ("embed", "ver-bbbbbbbbbbbbbbbb", "done", 1, None),
                    ("enrich", "ver-bbbbbbbbbbbbbbbb", "failed", 3, "RateLimitError"),
                ],
            )
            conn.executemany(
                "INSERT INTO egress_log (purpose, model, sent_targets) "
                "VALUES (?, ?, ?)",
                [
                    ("enrich", "claude", "[]"),
                    ("qa", "claude", "[]"),
                    ("qa", "claude", "[]"),
                ],
            )
    finally:
        conn.close()


def test_status_empty_db_reports_all_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "jobs: 0 pending, 0 running, 0 done, 0 failed" in result.stdout
    assert "egress: 0 sends (none)" in result.stdout
    assert "dead-letters (failed jobs): 0" in result.stdout


def test_status_summarizes_jobs_egress_and_dead_letters(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_jobs(db_path)
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "jobs: 1 pending, 1 running, 1 done, 1 failed" in result.stdout
    # Egress summary totals across purposes and breaks them out.
    assert "egress: 3 sends (enrich: 1, qa: 2)" in result.stdout
    # The single failed job surfaces as a dead-letter with its last error.
    assert "dead-letters (failed jobs): 1" in result.stdout
    assert "(enrich) target=ver-bbbbbbbb…: RateLimitError" in result.stdout


def test_jobs_empty_db_says_no_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["jobs", "--db", str(db_path)])
    assert result.exit_code == 0
    assert result.stdout.strip() == "no jobs"


def test_jobs_lists_every_job(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_jobs(db_path)
    result = runner.invoke(app, ["jobs", "--db", str(db_path)])
    assert result.exit_code == 0
    # One line per job (4 seeded), in id order, with type/status/attempts.
    assert len([ln for ln in result.stdout.splitlines() if ln.strip()]) == 4
    assert "embed" in result.stdout and "enrich" in result.stdout
    assert "target=ver-bbbbbbbb…" in result.stdout
    # The failed job carries its last error inline.
    assert "! RateLimitError" in result.stdout


def test_jobs_status_filter_narrows_to_one_state(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_jobs(db_path)
    result = runner.invoke(app, ["jobs", "--status", "failed", "--db", str(db_path)])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "failed" in lines[0]
    assert "! RateLimitError" in lines[0]


def test_jobs_rejects_unknown_status(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["jobs", "--status", "bogus", "--db", str(db_path)])
    assert result.exit_code != 0


# --- lode egress (lode-fk8.3) -----------------------------------------------


def _seed_egress(db_path: Path) -> None:
    """Seed egress_log rows spanning purposes, sent ids, and redactions."""
    conn = init_db(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO egress_log (purpose, model, sent_targets, redactions) "
                "VALUES (?, ?, ?, ?)",
                [
                    # enrich send: one long target id, no redactions.
                    ("enrich", "claude-haiku-4-5", '["ver-aaaaaaaaaaaaaaaa"]', None),
                    # qa send: two passages, one of them redacted twice.
                    (
                        "qa",
                        "claude-sonnet-4-6",
                        '["psg-bbbbbbbbbbbbbbbb", "psg-cccccccccccccccc"]',
                        '{"psg-bbbbbbbbbbbbbbbb": 2}',
                    ),
                ],
            )
    finally:
        conn.close()


def test_egress_empty_db_says_no_egress(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["egress", "--db", str(db_path)])
    assert result.exit_code == 0
    assert result.stdout.strip() == "no egress"


def test_egress_lists_every_send_with_ts_purpose_model_ids_redactions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    _seed_egress(db_path)
    result = runner.invoke(app, ["egress", "--db", str(db_path)])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    # One row per send (2 seeded).
    assert len(lines) == 2
    # The enrich send: purpose, model, its shortened sent id, no redactions.
    assert "enrich" in lines[0]
    assert "claude-haiku-4-5" in lines[0]
    assert "sent: ver-aaaaaaaa…" in lines[0]
    assert "redactions: none" in lines[0]
    # The qa send: both passage ids and the redaction count surface.
    assert "qa" in lines[1]
    assert "claude-sonnet-4-6" in lines[1]
    assert "sent: psg-bbbbbbbb…, psg-cccccccc…" in lines[1]
    assert "redactions: psg-bbbbbbbb…×2" in lines[1]
    # Every row carries a ts (the schema-default ISO-8601 UTC stamp: ...T...Z).
    assert all("T" in ln and "Z" in ln for ln in lines)


def test_egress_purpose_filter_narrows_to_one_purpose(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_egress(db_path)
    result = runner.invoke(app, ["egress", "--purpose", "qa", "--db", str(db_path)])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "qa" in lines[0]
    assert "enrich" not in result.stdout


def test_egress_rejects_unknown_purpose(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["egress", "--purpose", "bogus", "--db", str(db_path)])
    assert result.exit_code != 0


# --- lode purge (E8 hard delete via Repository.purge, lode-7cx) -------------


def test_purge_hard_deletes_a_note_and_reports_the_sweep(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "secret hunter2", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["purge", note_id, "--db", str(db_path)])
    assert result.exit_code == 0
    assert note_id in result.stdout  # it reports what it swept, not refuses

    # The body is overwritten with the [purged YYYY-MM-DD] marker and purged_at set.
    marker = f"[purged {datetime.now(timezone.utc):%Y-%m-%d}]"
    assert marker in result.stdout
    assert _rows(
        db_path,
        "SELECT body, purged_at IS NOT NULL FROM versions WHERE note_id = ?",
        (note_id,),
    ) == [(marker, 1)]


def test_purge_unknown_note_reports_and_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["purge", "ghost", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no such note" in result.stderr

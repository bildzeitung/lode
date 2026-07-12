"""Tests for the lode CLI.

Covers the skeleton surface (lode-txh.5: the subcommands exist, dispatch, and are
listed by ``--help``), the real ``lode add`` capture command (lode-y42.1) — it
persists via ``versions.save`` with no AI in the path, enqueues embed/enrich
derive jobs, refuses an empty note, and on a CAS reject preserves the buffer as a
draft rather than clobbering — the operational read-outs (lode-y42.3): ``status``
(job-queue health, dead-letters, egress summary), ``jobs`` (list/filter the derive
queue); the ``egress`` audit read-out (lode-fk8.3: per-send ts/purpose/model/sent
ids/redactions); ``purge`` (the E8 hard delete via ``Repository.purge``, lode-7cx);
``notes`` (lode-1gr.1: list every live note's full id/date/summary, the id source
for ``purge``); ``show`` (lode-1gr.5, brought to CONTENT parity with the TUI
inspector modal by lode-ay5.3: one note's head body + full derived enrichment --
summary/tags/entities/edges-with-reason-confidence via the shared
``lode.enrichment_view`` seam, plus a three-valued ``enrichment:`` line and embed
status -- sharing ``purge``'s id/prefix resolution); ``ask`` (the cited Q&A
loop,
lode-y42.2: retrieve → synthesize → faithfulness gate → cite or abstain, with the
Anthropic client mocked so the gate runs offline); and ``no-egress`` (lode-w0h.7:
the no-egress-tier control surface for an external source -- sets/``--clear``s
``externals.no_egress`` via ``lode.externals.set_no_egress``, refusing an unknown
external_id).
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lode import __version__, cli, config
from lode.answer import Claim, Support
from lode.cli import app
from lode.cited_answer import CitedAnswer
from lode.config import load_settings
from lode.egress import WithheldCitation
from lode.embedding import embed
from lode.externals import ingest_snapshot
from lode.hashing import NO_PARENT, content_version_id
from lode.ids import short_version_id
from lode.jobs import enqueue_derive_jobs
from lode.redact import REDACTION_MARKER
from lode.storage import init_db
from lode.versions import delete, save

runner = CliRunner()

# Every subcommand is real: `add` (lode-y42.1), `ask` (lode-y42.2), `status` /
# `jobs` (lode-y42.3), `egress` (lode-fk8.3), `purge` (lode-7cx), `notes` (lode-1gr.1),
# `config` (lode-ftc), `work` (lode-i05.3: async work queue drain).
# `eval` is NOT a shipped command — it is a maintainer/CI integration test run via
# `nox -s eval` (see docs/decisions.md, Shape A, lode-5y8.5).
ALL_SUBCOMMANDS = [
    "add",
    "ask",
    "purge",
    "notes",
    "show",
    "status",
    "jobs",
    "egress",
    "no-egress",
    "config",
    "work",
    "tui",
]


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


# --- lode --debug (top-level flag, lode-1i8.3) ------------------------------
#
# The group callback (main()) is the single place logging is configured
# (lode-txh.4): --debug resolves to an explicit DEBUG level, which takes
# precedence over the LODE_LOG_LEVEL env fallback; without --debug, the env
# fallback (default INFO) is unchanged. This is what turns on every
# DEBUG-gated diagnostic (e.g. the TUI's event-loop-lag latency_probe) across
# every subcommand, since the level set here persists for the rest of the
# process unless a subcommand (only `tui`, for the console-suppression
# interplay of lode-1i8.2) re-configures logging itself.


def test_debug_flag_sets_debug_log_level() -> None:
    result = runner.invoke(app, ["--debug", "version"])
    assert result.exit_code == 0
    assert logging.getLogger().level == logging.DEBUG


def test_without_debug_flag_env_fallback_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LODE_LOG_LEVEL", "WARNING")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert logging.getLogger().level == logging.WARNING


def test_debug_flag_takes_precedence_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODE_LOG_LEVEL", "WARNING")
    result = runner.invoke(app, ["--debug", "version"])
    assert result.exit_code == 0
    assert logging.getLogger().level == logging.DEBUG


def _spy_configure_logging(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch ``cli.configure_logging`` to record each call's level/console
    while still delegating to the real implementation, so the callback
    wiring is asserted without hand-rolling logging setup.
    """
    calls: list[dict] = []
    real_configure_logging = cli.configure_logging

    def _spy(*, level=None, log_dir=None, console=True):
        calls.append({"level": level, "console": console})
        return real_configure_logging(level=level, log_dir=log_dir, console=console)

    monkeypatch.setattr(cli, "configure_logging", _spy)
    return calls


def test_tui_debug_flag_propagates_to_file_only_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--debug must survive tui's second, file-only configure_logging call
    (the lode-1i8.2 interplay): the TUI never reattaches a console handler,
    so raising the level there only raises the log FILE's verbosity -- the
    console stays suppressed either way.
    """
    calls = _spy_configure_logging(monkeypatch)
    monkeypatch.setattr("lode.tui.app.run", lambda **kwargs: None)

    result = runner.invoke(app, ["--debug", "tui"])
    assert result.exit_code == 0
    assert calls == [
        {"level": logging.DEBUG, "console": True},
        {"level": logging.DEBUG, "console": False},
    ]


def test_tui_without_debug_omits_level_on_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_configure_logging(monkeypatch)
    monkeypatch.setattr("lode.tui.app.run", lambda **kwargs: None)

    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 0
    assert calls == [
        {"level": None, "console": True},
        {"level": None, "console": False},
    ]


# --- lode add ---------------------------------------------------------------


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_add_captures_note_and_enqueues_embed_and_enrich_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode add persists the note and enqueues both derive jobs (lode-npx.2).

    save() enqueues embed + enrich atomically; the capture path then
    opportunistically claims + runs the enrich job inline, so a successful
    immediate enrichment leaves it 'done' rather than 'pending'. run_one also
    stamps the done enrich job's own prompt_ver (lode-q47) -- embed's stays
    NULL by the schema's job-identity design.
    """
    import lode.enrich as enrich_mod

    def _fake_enrich(conn, version_id, settings, *, client=None):
        pass

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "hello world", "--db", str(db_path)])
    assert result.exit_code == 0
    note_id = result.stdout.strip()

    # The note and its root version are persisted via versions.save.
    assert _rows(
        db_path, "SELECT note_id, body, op FROM versions WHERE note_id = ?", (note_id,)
    ) == [(note_id, "hello world", "create")]

    # embed stays pending for the async worker; enrich was claimed + run inline.
    (version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    )[0]
    assert _rows(
        db_path,
        "SELECT type, status, prompt_ver FROM jobs WHERE target_version = ? "
        "ORDER BY type",
        (version_id,),
    ) == [("embed", "pending", None), ("enrich", "done", enrich_mod.ENRICH_PROMPT_VER)]


def test_add_calls_enrich_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode add claims + runs the enrich job immediately after saving (lode-npx.2).

    The capture path enriches the fresh note via a direct Haiku call so
    tags/entities/edges appear without waiting for the async worker.
    """

    calls: list[str] = []

    def _fake_enrich(conn, version_id, settings, *, client=None):
        calls.append(version_id)

    monkeypatch.setattr("lode.cli.enrich_version", _fake_enrich, raising=False)
    # Patch via the worker's deferred enrich-handler import path.
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "hello world", "--db", str(db_path)])
    assert result.exit_code == 0

    # enrich_version must have been called exactly once for the new version.
    assert len(calls) == 1


def test_add_claims_own_job_not_backlog_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Immediate-enrich claims THIS note's job, not an older backlog job (lode-a3x).

    Regression test for the bug that got lode-npx.2 bounced by /land's semantic
    review: ``_enrich_immediately`` took a ``version_id`` parameter but never
    used it -- it called ``claim_and_run_one`` with no version filter, which
    claims the OLDEST PENDING job of that type (FIFO via ``_claim_one``), not
    the job just enqueued for the note being saved. Under any backlog (a burst
    of prior adds, an idle worker), a fresh ``lode add`` could immediately
    enrich an unrelated older note instead of the one just saved. Every other
    test in this module starts from an empty DB, so the just-enqueued job was
    always coincidentally the only pending one -- this test seeds an unrelated
    pending enrich job first, backdated so it would win a naive FIFO claim, and
    asserts the NEW note's job -- not the backlog one -- is the one claimed and
    run.
    """
    import lode.enrich as enrich_mod

    seen_versions: list[str] = []

    def _fake_enrich(conn, version_id, settings, *, client=None):
        seen_versions.append(version_id)

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich)

    db_path = tmp_path / "lode.db"
    backlog_version = "backlog-version-id"
    conn = init_db(db_path)
    try:
        enqueue_derive_jobs(conn, backlog_version, types=("enrich",))
        # Backdate so this job would win an oldest-pending / FIFO claim if the
        # immediate-enrich claim were not scoped to the new note's version.
        with conn:
            conn.execute(
                "UPDATE jobs SET created = '2020-01-01T00:00:00.000Z' "
                "WHERE target_version = ?",
                (backlog_version,),
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["add", "hello world", "--db", str(db_path)])
    assert result.exit_code == 0
    note_id = result.stdout.strip()

    (version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    )[0]
    assert version_id != backlog_version

    # enrich_version ran exactly once, for the NEW note's version -- never for
    # the backlog job.
    assert seen_versions == [version_id]

    # The new note's enrich job was claimed + run to 'done'; the backlog job is
    # untouched, still 'pending' for the async worker to pick up later.
    assert _rows(
        db_path,
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (version_id,),
    ) == [("embed", "pending"), ("enrich", "done")]
    assert _rows(
        db_path, "SELECT status FROM jobs WHERE target_version = ?", (backlog_version,)
    ) == [("pending",)]


def test_add_enrich_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Immediate enrichment failure does not abort the capture (lode-npx.2).

    The embed job still lands and the note is saved even if Haiku is
    unreachable; the failed enrich job's own backoff/dead-letter accounting
    (worker.run_one) takes over — no separate re-enqueue path needed.
    """
    import lode.enrich as enrich_mod

    def _boom(conn, version_id, settings, *, client=None):
        raise RuntimeError("API down")

    monkeypatch.setattr(enrich_mod, "enrich_version", _boom)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "note body", "--db", str(db_path)])
    # Capture must succeed despite enrichment failure.
    assert result.exit_code == 0
    note_id = result.stdout.strip()
    assert note_id

    # Both jobs were enqueued; the claimed-and-run enrich job is now 'failed'
    # (attempt 1 of retry_max_attempts) rather than 'pending' or 'done'.
    (version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    )[0]
    jobs_by_type = {
        r[0]: (r[1], r[2])
        for r in _rows(
            db_path,
            "SELECT type, status, attempts FROM jobs WHERE target_version = ?",
            (version_id,),
        )
    }
    assert jobs_by_type["embed"] == ("pending", 0)
    assert jobs_by_type["enrich"] == ("failed", 1)


def test_add_reads_body_from_stdin_verbatim(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "--db", str(db_path)], input="from stdin\n")
    assert result.exit_code == 0
    note_id = result.stdout.strip()
    # Stored verbatim — the trailing newline is preserved, not stripped.
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (note_id,)
    ) == [("from stdin\n",)]


def test_add_uses_lode_home_env_var(tmp_path: Path) -> None:
    # $LODE_HOME is the single root: with no --db, `add` writes $LODE_HOME/lode.db
    # (lode-qd9, replacing the old $LODE_DB binding).
    home = tmp_path / "home"
    result = runner.invoke(app, ["add", "via env"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    assert (home / "lode.db").exists()


def test_add_logs_land_under_lode_home(tmp_path: Path) -> None:
    # Acceptance: logs land in $LODE_HOME/logs/ (lode-qd9). The group callback
    # attaches a file handler there on every command.
    home = tmp_path / "home"
    result = runner.invoke(app, ["add", "logged"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    assert (home / "logs").is_dir()


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
    """Seed a spread of job rows + egress_log rows to read back via status/jobs.

    Uses 'dead' (not 'failed') as the dead-letter terminal row — 'failed' is the
    transient last-error state retried by the worker; 'dead' is the terminal
    poison state at max-attempts (lode-i05.6).
    """
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
                    ("enrich", "ver-bbbbbbbbbbbbbbbb", "dead", 3, "RateLimitError"),
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
    assert "jobs: 0 pending, 0 running, 0 done, 0 failed, 0 dead" in result.stdout
    assert "egress: 0 sends (none)" in result.stdout
    assert "dead-letters (dead jobs): 0" in result.stdout


def test_status_summarizes_jobs_egress_and_dead_letters(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed_jobs(db_path)
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    # Seed has: 1 pending, 1 running, 1 done, 0 failed, 1 dead.
    assert "jobs: 1 pending, 1 running, 1 done, 0 failed, 1 dead" in result.stdout
    # Egress summary totals across purposes and breaks them out.
    assert "egress: 3 sends (enrich: 1, qa: 2)" in result.stdout
    # The single dead job surfaces as a dead-letter with its last error.
    assert "dead-letters (dead jobs): 1" in result.stdout
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
    # Filter on 'dead' (the dead-letter terminal; 'failed' is the transient state).
    result = runner.invoke(app, ["jobs", "--status", "dead", "--db", str(db_path)])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "dead" in lines[0]
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


# --- lode no-egress (the no-egress-tier control surface, lode-w0h.7) --------


def test_no_egress_marks_an_existing_external(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        ingest_snapshot(conn, "https://example.com/a", "web", "body")
    finally:
        conn.close()

    result = runner.invoke(
        app, ["no-egress", "https://example.com/a", "--db", str(db_path)]
    )
    assert result.exit_code == 0
    assert "marked no_egress" in result.stdout
    assert _rows(
        db_path,
        "SELECT no_egress FROM externals WHERE external_id = ?",
        ("https://example.com/a",),
    ) == [(1,)]


def test_no_egress_clear_flips_it_back(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        ingest_snapshot(conn, "https://example.com/a", "web", "body")
    finally:
        conn.close()
    runner.invoke(app, ["no-egress", "https://example.com/a", "--db", str(db_path)])

    result = runner.invoke(
        app,
        ["no-egress", "https://example.com/a", "--clear", "--db", str(db_path)],
    )
    assert result.exit_code == 0
    assert "cleared no_egress" in result.stdout
    assert _rows(
        db_path,
        "SELECT no_egress FROM externals WHERE external_id = ?",
        ("https://example.com/a",),
    ) == [(0,)]


def test_no_egress_unknown_external_reports_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(
        app, ["no-egress", "https://never-ingested.example", "--db", str(db_path)]
    )
    assert result.exit_code == 1
    assert "no such external source" in result.stderr


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


# --- lode notes (list live notes, lode-1gr.1) -------------------------------


def test_notes_empty_db_says_no_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["notes", "--db", str(db_path)])
    assert result.exit_code == 0
    assert result.stdout.strip() == "no notes"


def test_notes_lists_the_full_id_date_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserts against the pre-enrichment first-line fallback (lode-16g).

    ``add``'s immediate-enrich fast path (lode-npx.2) claims + runs the
    freshly enqueued enrich job inline, in-process -- so without stubbing it
    here this test races the real ``enrich_version`` call: whether it
    produces a summary annotation before ``notes`` reads the row is
    non-deterministic (network/credential dependent), and when it does, the
    summary column no longer contains the raw first line this test asserts
    on. Stubbing it to a no-op -- the same pattern
    ``test_add_captures_note_and_enqueues_embed_and_enrich_jobs`` and
    ``test_add_calls_enrich_immediately`` already use -- pins the note to its
    not-yet-enriched state, where ``notes_read.list_notes`` falls back to the
    note's first line.
    """
    import lode.enrich as enrich_mod

    def _fake_enrich(conn, version_id, settings, *, client=None):
        pass

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich)

    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "the first line of the note", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["notes", "--db", str(db_path)])

    assert result.exit_code == 0
    assert note_id in result.stdout  # full id, copy-pasteable into `purge`
    assert "the first line of the note" in result.stdout
    created = _rows(db_path, "SELECT created FROM notes WHERE note_id = ?", (note_id,))[
        0
    ][0]
    assert created[:16].replace("T", " ") in result.stdout


def test_notes_excludes_a_tombstoned_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "gone soon", "--db", str(db_path)]
    ).stdout.strip()
    (head_version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    )[0]
    conn = init_db(db_path)
    try:
        delete(conn, note_id, parent=head_version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["notes", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "no notes"


# --- lode notes --deleted (list tombstoned notes, lode-d32.2) ---------------


def test_notes_deleted_flag_lists_only_tombstoned_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    live_id = runner.invoke(
        app, ["add", "still here", "--db", str(db_path)]
    ).stdout.strip()
    gone_id = runner.invoke(
        app, ["add", "gone soon", "--db", str(db_path)]
    ).stdout.strip()
    (head_version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (gone_id,)
    )[0]
    conn = init_db(db_path)
    try:
        delete(conn, gone_id, parent=head_version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["notes", "--deleted", "--db", str(db_path)])

    assert result.exit_code == 0
    assert gone_id in result.stdout  # full id -- the only route to `show`/`recover`
    assert live_id not in result.stdout
    assert "gone soon" in result.stdout


def test_notes_deleted_flag_says_no_deleted_notes_when_none_are_tombstoned(
    tmp_path: Path,
) -> None:
    """The empty message names the queried scope -- a live note exists here."""
    db_path = tmp_path / "lode.db"
    runner.invoke(app, ["add", "still here", "--db", str(db_path)])

    result = runner.invoke(app, ["notes", "--deleted", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "no deleted notes"


# --- lode purge <prefix> (unambiguous note-id prefix, lode-1gr.3) -----------


def test_purge_accepts_an_unambiguous_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "secret hunter2", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["purge", note_id[:8], "--db", str(db_path)])
    assert result.exit_code == 0
    assert note_id in result.stdout  # resolved to the full id in the report

    assert _rows(
        db_path,
        "SELECT purged_at IS NOT NULL FROM versions WHERE note_id = ?",
        (note_id,),
    ) == [(1,)]


def test_purge_ambiguous_prefix_reports_candidates_and_purges_nothing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-aaa111", "body a")
        save(conn, "note-aaa222", "body b")
    finally:
        conn.close()

    result = runner.invoke(app, ["purge", "note-aaa", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr
    assert "note-aaa111" in result.stderr
    assert "note-aaa222" in result.stderr

    # Neither candidate was touched.
    assert _rows(
        db_path,
        "SELECT purged_at FROM versions WHERE note_id IN "
        "('note-aaa111', 'note-aaa222')",
    ) == [(None,), (None,)]


def test_purge_prefix_does_not_match_a_tombstoned_note(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        from lode.versions import delete

        result = save(conn, "note-aaa111", "body a")
        delete(conn, "note-aaa111", parent=result.version_id)
    finally:
        conn.close()

    # The tombstoned note isn't reachable by prefix (it's not in Browse
    # either) — same "no such note" as an unknown prefix.
    result = runner.invoke(app, ["purge", "note-aaa", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no such note" in result.stderr


def test_purge_empty_prefix_purges_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "the only note", "--db", str(db_path)]
    ).stdout.strip()

    # `lode purge ""` must not sweep the sole live note — an empty string is
    # not an unambiguous prefix; it errors like any unknown id (lode-1gr.3).
    result = runner.invoke(app, ["purge", "", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no such note" in result.stderr

    assert _rows(
        db_path,
        "SELECT purged_at FROM versions WHERE note_id = ?",
        (note_id,),
    ) == [(None,)]


def test_purge_full_id_still_works_regardless_of_note_state(
    tmp_path: Path,
) -> None:
    """Full-id (36-char) purge is unchanged: it still reaches a tombstone."""
    db_path = tmp_path / "lode.db"
    full_id = "a" * 36  # a real uuid4 is also 36 chars; only the length matters
    conn = init_db(db_path)
    try:
        from lode.versions import delete

        result = save(conn, full_id, "body a")
        delete(conn, full_id, parent=result.version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["purge", full_id, "--db", str(db_path)])
    assert result.exit_code == 0
    # Both chain versions (create + delete tombstone) are purged.
    assert _rows(
        db_path,
        "SELECT purged_at IS NOT NULL FROM versions WHERE note_id = ?",
        (full_id,),
    ) == [(1,), (1,)]


# --- lode show (per-note detail + derived enrichment, lode-1gr.5) ----------


def test_show_unenriched_note_prints_body_and_empty_annotation_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly-added, not-yet-enriched note shows the body + '(none)' sections.

    ``add`` writes passages synchronously (the lexical/FTS leg, lode-xyb) even
    with enrichment stubbed out below -- so this note is already indexed;
    what's un-enriched is the Haiku-derived layer (summary/tags/entities/edges).
    """
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", lambda *a, **k: None)

    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "an un-enriched note", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["show", note_id, "--db", str(db_path)])

    assert result.exit_code == 0
    assert note_id in result.stdout
    assert "an un-enriched note" in result.stdout
    assert "summary: (none)" in result.stdout
    assert "tags: (none)" in result.stdout
    assert "entities: (none)" in result.stdout
    assert "edges: (none)" in result.stdout


def test_show_reports_not_embedded_when_no_passages_exist(tmp_path: Path) -> None:
    """A note saved without going through the cache-wired Repository (no lexical
    write) has no passages yet -- ``show`` reports it plainly, not an error.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-show-3", "not yet indexed")
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-show-3", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "embedded: no (0 passage(s))" in result.stdout


def test_show_prints_enrichment_and_embed_status(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-show-1", "the note body")
        head_version_id = result.version_id
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'summary', ?, 'ai', 'fresh')",
            ("note-show-1", head_version_id, json.dumps("a one-line summary")),
        )
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'tag', ?, 'ai', 'fresh')",
            ("note-show-1", head_version_id, json.dumps("python")),
        )
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'entity', ?, 'ai', 'fresh')",
            ("note-show-1", head_version_id, json.dumps("Anthropic")),
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'concept-x', 'ai', 'because', 0.9, ?, 'fresh')",
            ("note-show-1", head_version_id),
        )
        conn.execute(
            "INSERT INTO passages (passage_id, target_version, ord, text) "
            "VALUES ('p1', ?, 0, 'the note body')",
            (head_version_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-show-1", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "the note body" in result.stdout
    assert "summary: a one-line summary" in result.stdout
    assert "tags: python" in result.stdout
    assert "entities: Anthropic" in result.stdout
    assert "concept-x" in result.stdout
    assert "embedded: yes (1 passage(s))" in result.stdout


def test_show_renders_edge_reason_and_confidence_compact(tmp_path: Path) -> None:
    """Edge parity (lode-ay5.3): 'lode show' gains reason+confidence, compact.

    Pre-ay5.3, `show` printed only `-> to_id[stale]` even though both columns
    exist on `edges`. This is the net-new CLI field the epic's parity decision
    (2026-07-08) requires.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-show-edge", "body")
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'concept-x', 'ai', 'mentions jwt auth', 0.82, ?, 'fresh')",
            ("note-show-edge", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-show-edge", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "-> concept-x (mentions jwt auth, 0.82)" in result.stdout


def test_show_edge_with_no_reason_or_confidence_omits_the_parenthetical(
    tmp_path: Path,
) -> None:
    """A user-curated edge (reason/confidence both NULL, per schema.sql) degrades
    to the old bare '-> to_id' form rather than printing an empty '()'."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-show-user-edge", "body")
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, source_version, status) "
            "VALUES (?, 'concept-y', 'user', ?, 'fresh')",
            ("note-show-user-edge", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-show-user-edge", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "-> concept-y\n" in result.stdout
    assert "concept-y (" not in result.stdout


def test_show_enrichment_state_pending_failed_ready_are_wording_distinct(
    tmp_path: Path,
) -> None:
    """The three-valued enrichment_state (lode-ay5.1) renders distinctly on the
    CLI: 'pending' / 'failed' / 'ready' replace the old ambiguous bare '(none)'
    that couldn't tell not-yet-enriched from enriched-empty from dead-lettered.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        pending = save(conn, "note-pending", "not yet enriched")
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) "
            "VALUES ('enrich', ?, 'pending')",
            (pending.version_id,),
        )

        failed = save(conn, "note-failed", "enrich dead-lettered")
        conn.execute(
            "INSERT INTO jobs (type, target_version, status) "
            "VALUES ('enrich', ?, 'dead')",
            (failed.version_id,),
        )

        # 'note-show-1' style: no job row at all -> ready (never enriched, or
        # already finished) -- covered separately by the un-enriched/enriched
        # tests above; here just confirm a third, bare note reads 'ready'.
        save(conn, "note-ready", "genuinely no job")
        conn.commit()
    finally:
        conn.close()

    pending_out = runner.invoke(
        app, ["show", "note-pending", "--db", str(db_path)]
    ).stdout
    failed_out = runner.invoke(
        app, ["show", "note-failed", "--db", str(db_path)]
    ).stdout
    ready_out = runner.invoke(app, ["show", "note-ready", "--db", str(db_path)]).stdout

    assert "enrichment: pending" in pending_out
    assert "enrichment: failed" in failed_out
    assert "enrichment: ready" in ready_out
    # Distinct wording, not a shared ambiguous placeholder.
    states = {"enrichment: pending", "enrichment: failed", "enrichment: ready"}
    assert len(states) == 3


def test_show_field_coverage_matches_the_view_model(tmp_path: Path) -> None:
    """Content-parity guard (lode-ay5.3, epic decision 2026-07-08): parity is

    checked by field-coverage against the shared view-model, not by diffing
    'lode show' output against the TUI modal's exact bytes. This enumerates
    EnrichmentView's own fields and asserts 'lode show' surfaces each one.
    """
    import dataclasses

    from lode.enrichment_view import EnrichmentView

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-parity", "parity body")
        head_version_id = result.version_id
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'summary', ?, 'ai', 'fresh')",
            ("note-parity", head_version_id, json.dumps("a summary")),
        )
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'tag', ?, 'ai', 'fresh')",
            ("note-parity", head_version_id, json.dumps("a-tag")),
        )
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'entity', ?, 'ai', 'fresh')",
            ("note-parity", head_version_id, json.dumps("an-entity")),
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'concept-parity', 'ai', 'because', 0.5, ?, 'fresh')",
            ("note-parity", head_version_id),
        )
        conn.execute(
            "INSERT INTO passages (passage_id, target_version, ord, text) "
            "VALUES ('p-parity', ?, 0, 'parity body')",
            (head_version_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-parity", "--db", str(db_path)])
    assert result.exit_code == 0
    stdout = result.stdout

    field_names = {f.name for f in dataclasses.fields(EnrichmentView)}
    assert field_names == {
        "note_id",
        "enrichment_state",
        "summary",
        "tags",
        "entities",
        "edges",
        "embedded",
        "passage_count",
    }
    # note_id -> the header line; enrichment_state -> the 'enrichment:' line;
    # summary/tags/entities/edges -> their own sections; embedded +
    # passage_count -> the combined 'embedded: yes (N passage(s))' line.
    assert "note_id: note-parity" in stdout
    assert "enrichment: ready" in stdout
    assert "summary: a summary" in stdout
    assert "tags: a-tag" in stdout
    assert "entities: an-entity" in stdout
    assert "-> concept-parity (because, 0.50)" in stdout
    assert "embedded: yes (1 passage(s))" in stdout


def test_show_flags_stale_enrichment_rather_than_hiding_it(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-show-2", "body v1")
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status) "
            "VALUES (?, ?, 'tag', ?, 'ai', 'stale')",
            ("note-show-2", result.version_id, json.dumps("old-tag")),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-show-2", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "old-tag [stale]" in result.stdout


def test_show_accepts_an_unambiguous_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "prefix me", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["show", note_id[:8], "--db", str(db_path)])
    assert result.exit_code == 0
    assert note_id in result.stdout
    assert "prefix me" in result.stdout


def test_show_ambiguous_prefix_reports_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-bbb111", "body a")
        save(conn, "note-bbb222", "body b")
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-bbb", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr
    assert "note-bbb111" in result.stderr
    assert "note-bbb222" in result.stderr


def test_show_unknown_note_reports_and_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["show", "ghost", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no such note" in result.stderr


def test_show_unknown_full_id_reports_no_such_note(tmp_path: Path) -> None:
    """A full-length (36-char) id that matches no note is 'no such note', not a crash."""
    db_path = tmp_path / "lode.db"
    full_id = "b" * 36
    result = runner.invoke(app, ["show", full_id, "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no such note" in result.stderr


def test_show_flags_a_tombstoned_note_with_deleted_marker(tmp_path: Path) -> None:
    """A tombstoned note reads as such rather than as if live (lode-d32.2).

    ``resolve_note_prefix`` is scoped to live notes (repository.py), so a
    tombstoned note is only reachable by its full 36-char id -- same
    full-id-bypasses-resolution case as
    ``test_purge_full_id_still_works_regardless_of_note_state``.
    """
    db_path = tmp_path / "lode.db"
    full_id = "c" * 36  # a real uuid4 is also 36 chars; only the length matters
    conn = init_db(db_path)
    try:
        result = save(conn, full_id, "the carried-forward body")
        delete(conn, full_id, parent=result.version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["show", full_id, "--db", str(db_path)])

    assert result.exit_code == 0
    assert f"note_id: {full_id} [deleted]" in result.stdout
    # The tombstone's carried-forward body is still shown -- useful context
    # for deciding whether to recover it.
    assert "the carried-forward body" in result.stdout


def test_show_live_note_has_no_deleted_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "still alive", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["show", note_id, "--db", str(db_path)])

    assert result.exit_code == 0
    assert f"note_id: {note_id}" in result.stdout
    assert "[deleted]" not in result.stdout


# --- lode show: external-snapshot introspection (lode-8d2) -----------------


def _seed_external(
    conn: sqlite3.Connection,
    *,
    external_id: str = "https://example.com/article",
    source_type: str = "web",
    snapshot_id: str = "snap-cli-1",
    status: str = "ok",
    no_egress: bool = False,
    fetched_at: str = "2026-07-08T00:00:00.000000Z",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) "
            "VALUES (?, ?, ?)",
            (external_id, source_type, int(no_egress)),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, status, fetched_at) "
            "VALUES (?, ?, 'body', ?, ?)",
            (snapshot_id, external_id, status, fetched_at),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )


def test_show_renders_external_snapshot_introspection_for_a_drawn_down_link(
    tmp_path: Path,
) -> None:
    """A note whose edge draws down a web link shows that external's snapshot
    (source_type/snapshot id/fetched_at/state) indented beneath the edge line
    -- the CLI half of lode-8d2, through the same ay5.1 seam the TUI modal
    consumes.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-external-1", "see https://example.com/article")
        _seed_external(conn, snapshot_id="snap-cli-1")
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'https://example.com/article', 'user', 'pasted URL', "
            "1.0, ?, 'fresh')",
            ("note-external-1", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-external-1", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "-> https://example.com/article (pasted URL, 1.00)" in result.stdout
    assert f"snapshot {short_version_id('snap-cli-1')}" in result.stdout
    assert "web" in result.stdout
    assert "as of 2026-07-08T00:00:00.000000Z" in result.stdout
    assert "[un-refreshed]" in result.stdout


def test_show_marks_a_tombstoned_external_stale(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-external-2", "see https://dead.example.com/")
        _seed_external(
            conn,
            external_id="https://dead.example.com/",
            snapshot_id="snap-cli-dead",
            status="tombstone",
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'https://dead.example.com/', 'user', 'pasted URL', "
            "1.0, ?, 'fresh')",
            ("note-external-2", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-external-2", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "[stale]" in result.stdout


def test_show_marks_a_no_egress_external_withheld(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-external-3", "see https://sensitive.example.com/")
        _seed_external(
            conn,
            external_id="https://sensitive.example.com/",
            snapshot_id="snap-cli-sensitive",
            no_egress=True,
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'https://sensitive.example.com/', 'user', 'pasted URL', "
            "1.0, ?, 'fresh')",
            ("note-external-3", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-external-3", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "[withheld]" in result.stdout


def test_show_edge_without_a_matching_external_row_has_no_snapshot_line(
    tmp_path: Path,
) -> None:
    """An ordinary inferred edge (no externals row for its to_id) prints just
    the one edge line -- no extra snapshot line, no crash."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-no-external", "body")
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, 'concept-not-external', 'ai', 'because', 0.5, ?, 'fresh')",
            ("note-no-external", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", "note-no-external", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "-> concept-not-external (because, 0.50)" in result.stdout
    assert "snapshot" not in result.stdout


# --- lode recover <prefix> (undo a soft-delete, lode-d32.3) ----------------


def test_recover_round_trip_reappears_in_notes(tmp_path: Path) -> None:
    """delete -> recover: the note comes back into 'lode notes' and its FTS row."""
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "coming back soon", "--db", str(db_path)]
    ).stdout.strip()
    (head_version_id,) = _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    )[0]
    conn = init_db(db_path)
    try:
        delete(conn, note_id, parent=head_version_id)
    finally:
        conn.close()

    # Gone from the live listing while tombstoned.
    assert runner.invoke(app, ["notes", "--db", str(db_path)]).stdout.strip() == (
        "no notes"
    )

    result = runner.invoke(app, ["recover", note_id, "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert note_id in result.stdout
    assert head_version_id in result.stdout  # head repointed past the tombstone

    # Head is repointed past the tombstone, back to the pre-delete version.
    assert _rows(
        db_path, "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
    ) == [(head_version_id,)]

    # Reappears in the live listing.
    notes_result = runner.invoke(app, ["notes", "--db", str(db_path)])
    assert note_id in notes_result.stdout

    # The FTS row is restored -- the write-path cache composite
    # (CompositeCache([LexicalCacheBackend(conn)])), not purge's bare
    # NullCache, is what recover must use (d32.2 land-review decision).
    assert _rows(
        db_path,
        "SELECT COUNT(*) FROM passages_fts WHERE target_version = ?",
        (head_version_id,),
    ) == [(1,)]


def test_recover_accepts_an_unambiguous_prefix_of_a_deleted_note(
    tmp_path: Path,
) -> None:
    """A tombstoned note IS reachable by prefix with include_deleted=True."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-ccc111", "gone soon")
        delete(conn, "note-ccc111", parent=result.version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["recover", "note-ccc", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "note-ccc111" in result.stdout

    (op,) = _rows(
        db_path,
        "SELECT v.op FROM notes n JOIN versions v ON v.version_id = n.head_version_id "
        "WHERE n.note_id = ?",
        ("note-ccc111",),
    )[0]
    assert op == "create"


def test_recover_live_note_errors_clearly(tmp_path: Path) -> None:
    """Recovering a note that isn't tombstoned errors -- nothing to recover."""
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "still alive", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["recover", note_id, "--db", str(db_path)])
    assert result.exit_code == 1
    assert "not deleted" in result.stderr

    # Untouched: still live, head unchanged.
    assert runner.invoke(app, ["notes", "--db", str(db_path)]).stdout.strip() != (
        "no notes"
    )


def test_recover_unknown_note_reports_and_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["recover", "ghost", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no such note" in result.stderr


def test_recover_ambiguous_prefix_across_live_and_deleted_candidates(
    tmp_path: Path,
) -> None:
    """A prefix matching one live + one deleted note is still ambiguous.

    recover does not get to silently prefer the tombstone (d32.2 land-review
    decision) -- this is the same ambiguity error purge/show already raise.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-ddd111", "still live")
        result = save(conn, "note-ddd222", "gone soon")
        delete(conn, "note-ddd222", parent=result.version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["recover", "note-ddd", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr
    assert "note-ddd111" in result.stderr
    assert "note-ddd222" in result.stderr


def test_recover_ambiguous_prefix_across_two_deleted_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        r1 = save(conn, "note-eee111", "gone a")
        delete(conn, "note-eee111", parent=r1.version_id)
        r2 = save(conn, "note-eee222", "gone b")
        delete(conn, "note-eee222", parent=r2.version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["recover", "note-eee", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr
    assert "note-eee111" in result.stderr
    assert "note-eee222" in result.stderr


def test_recover_full_id_of_a_live_note_errors_not_deleted(tmp_path: Path) -> None:
    """A full id also goes through the 'is it tombstoned' check, not just a prefix."""
    db_path = tmp_path / "lode.db"
    full_id = "d" * 36
    conn = init_db(db_path)
    try:
        save(conn, full_id, "body")
    finally:
        conn.close()

    result = runner.invoke(app, ["recover", full_id, "--db", str(db_path)])
    assert result.exit_code == 1
    assert "not deleted" in result.stderr


# --- lode ask (cited Q&A loop, lode-y42.2) ----------------------------------


class _FakeMessages:
    """Records every parse() call and returns a fixed parsed claims envelope."""

    def __init__(self, claims: list[Claim]) -> None:
        self._claims = claims
        self.calls: list[dict] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=SimpleNamespace(claims=self._claims))


class _FakeClient:
    """Stand-in for anthropic.Anthropic — no network, just records the call."""

    def __init__(self, claims: list[Claim]) -> None:
        self.messages = _FakeMessages(claims)


def _mock_qa(monkeypatch: pytest.MonkeyPatch, claims: list[Claim]) -> _FakeClient:
    """Mock the Q&A SDK client so cited_answer.ask runs offline; return the client."""
    client = _FakeClient(claims)
    monkeypatch.setattr("lode.qa.build_client", lambda: client)
    return client


class _ConstantEmbedder:
    """Offline stand-in for the embedder: every text maps to one fixed direction.

    ``ask``'s dense leg constructs :class:`lode.embedding.FastEmbedEmbedder` (a
    model download) unless one is injected, so the ``ask`` tests monkeypatch this in
    its place to keep the gate offline. A single fixed direction means any query is a
    perfect cosine match for any passage it indexed — so when vectors *are* present
    the dense leg surfaces them, and when the store is empty it simply contributes
    nothing. The dimension follows ``settings`` so the query vector matches the
    LanceDB table's width.
    """

    def __init__(self, settings) -> None:
        self._dim = settings.embedding_vector_dim

    def _vector(self) -> list[float]:
        return [1.0] + [0.0] * (self._dim - 1)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector() for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector()


def _offline_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the default ONNX embedder for :class:`_ConstantEmbedder` (no download)."""
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)


# ``_offline_embedder`` stubs only the embedder — it leaves ``lode.retrieval``'s
# real, un-mocked ``FastEmbedCrossEncoder`` in place, so any ``ask``/``retrieve``
# test whose corpus has candidates left to rerank pays that real model-load cost
# (``pytest --durations`` measured several seconds per test, lode-pql). Those are
# ``@pytest.mark.slow``; tests that mock the reranker directly (e.g. via
# ``_InvertingCrossEncoder`` below) or that hit an empty/no-candidate corpus never
# reach the real reranker and stay unmarked. See ``docs/onboarding.md`` for the
# fast (``nox -s unit``) vs. full (``nox -s tests``) tiers this feeds.


def _seed_corpus(
    db_path: Path, *, note_id: str, version_id: str, body: str, passage_id: str
) -> None:
    """Seed one note (head) plus the passage + FTS rows retrieval reads.

    Mirrors what the capture-side indexing will populate once it is wired (the
    ``passages`` table from the embed leg, the ``passages_fts`` row from the
    synchronous lexical leg), so ``ask``'s retrieval has a live head to find.
    """
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO notes (note_id, head_version_id, no_egress) VALUES (?, NULL, 0)",
            (note_id,),
        )
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES (?, ?, ?, 'create')",
            (version_id, note_id, body),
        )
        conn.execute(
            "UPDATE notes SET head_version_id = ? WHERE note_id = ?",
            (version_id, note_id),
        )
        conn.execute(
            "INSERT INTO passages "
            "(passage_id, target_version, ord, char_range, text, parent_block) "
            "VALUES (?, ?, 0, ?, ?, ?)",
            (passage_id, version_id, f"0:{len(body)}", body, body),
        )
        conn.execute(
            "INSERT INTO passages_fts (passage_id, target_version, text) "
            "VALUES (?, ?, ?)",
            (passage_id, version_id, body),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.slow
def test_ask_retrieves_and_renders_a_cited_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    body = "We decided to use OAuth for service auth."
    _seed_corpus(db_path, note_id="n1", version_id="v1", body=body, passage_id="p1")
    _offline_embedder(monkeypatch)
    # The model's claim cites v1 with a span verbatim in the body, and its payload
    # lies inside that span (extractive coupling), so it survives the faithfulness
    # gate and renders with its citation.
    client = _mock_qa(
        monkeypatch,
        [
            Claim(
                text="use OAuth",
                support=[Support(version_id="v1", quoted_span="use OAuth")],
            )
        ],
    )

    conn = init_db(db_path)
    try:
        (created,) = conn.execute(
            "SELECT created FROM versions WHERE version_id = 'v1'"
        ).fetchone()
    finally:
        conn.close()

    result = runner.invoke(
        app, ["ask", "what did we decide about auth?", "--db", str(db_path)]
    )

    assert result.exit_code == 0
    assert "use OAuth" in result.stdout
    assert "version_id v1" in result.stdout
    assert f"as of {created}" in result.stdout
    assert '"use OAuth"' in result.stdout
    # Retrieval actually fed the cited context to the Q&A send (v1's body reached it).
    (call,) = client.messages.calls
    assert "OAuth" in call["messages"][0]["content"]


@pytest.mark.slow
def test_ask_renders_as_of_fetched_at_for_a_cited_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (lode-w0h.4): a claim citing a mirrored external shows its
    ``fetched_at`` ("as of ..."), never a bare present-tense claim
    (``docs/externals.md`` "Every AI claim from an external must cite 'as of
    fetched_at'"). The snapshot is ingested through the real write path
    (:func:`lode.externals.ingest_snapshot`) rather than a hand-inserted row, so
    this exercises the citation line against real ingested external data.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        ingested = ingest_snapshot(
            conn,
            "https://example.com/JIRA-1",
            "web",
            "The ticket is open, waiting on review.",
        )
        (fetched_at,) = conn.execute(
            "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
            (ingested.snapshot_id,),
        ).fetchone()
    finally:
        conn.close()
    _offline_embedder(monkeypatch)
    # The model's claim cites the snapshot with a span verbatim in its body, so it
    # survives the faithfulness gate and renders with its citation.
    _mock_qa(
        monkeypatch,
        [
            Claim(
                text="ticket is open",
                support=[
                    Support(
                        snapshot_id=ingested.snapshot_id, quoted_span="ticket is open"
                    )
                ],
            )
        ],
    )

    result = runner.invoke(app, ["ask", "is the ticket open?", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "ticket is open" in result.stdout
    assert f"snapshot_id {ingested.snapshot_id}" in result.stdout
    assert f"as of {fetched_at}" in result.stdout


@pytest.mark.slow
def test_ask_abstains_when_no_claim_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    body = "We decided to use OAuth for service auth."
    _seed_corpus(db_path, note_id="n1", version_id="v1", body=body, passage_id="p1")
    _offline_embedder(monkeypatch)
    # The model asserts nothing — the gate abstains, the honest failure mode.
    _mock_qa(monkeypatch, [])

    result = runner.invoke(app, ["ask", "what about auth?", "--db", str(db_path)])

    assert result.exit_code == 0
    assert cli._ABSTAIN_LINE in result.stdout


def test_ask_out_of_corpus_question_abstains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()  # empty corpus: nothing to retrieve
    _offline_embedder(monkeypatch)
    _mock_qa(monkeypatch, [])

    result = runner.invoke(app, ["ask", "anything at all?", "--db", str(db_path)])

    assert result.exit_code == 0
    assert cli._ABSTAIN_LINE in result.stdout


@pytest.mark.slow
def test_retrieve_dense_leg_surfaces_a_vector_only_match(tmp_path: Path) -> None:
    """A passage matched only by the dense leg still reaches the Q&A context (lode-bkc).

    The question shares no word tokens with the note's body, so the lexical leg
    cannot find it; the only path to retrieval is the dense leg. Indexing the note
    through the embed leg with a constant-direction stub embedder makes the query's
    embedding a cosine match for the indexed passages, so the version surfaces in the
    trust-ranked context — proving ``_retrieve`` fuses the dense leg, not lexical
    alone. The small vector dim keeps the LanceDB table trivial; the stub keeps it
    offline.
    """
    settings = load_settings(embedding_vector_dim=4)
    db_path = tmp_path / "lode.db"
    lance_dir = config.lance_dir(db_path)
    conn = init_db(db_path)
    try:
        body = "alpha bravo charlie delta echo foxtrot"
        version = save(conn, "n1", body, settings=settings).version_id
        # Index passages + vectors for the head through the embed leg, so the dense
        # store and the SQLite passages rows (parent-expansion reads them) exist.
        embedder = _ConstantEmbedder(settings)
        assert (
            embed(
                conn, version, lance_dir=lance_dir, embedder=embedder, settings=settings
            )
            > 0
        )

        # A lexically disjoint question: the lexical leg matches nothing, so only the
        # dense leg can surface this note.
        context = cli._retrieve(
            conn,
            "unrelated wording entirely",
            lance_dir=lance_dir,
            embedder=embedder,
            settings=settings,
        )

        assert version in {item.target_version for item in context}
    finally:
        conn.close()


class _OneDirEmbedder:
    """Offline stub: every text embeds to a single fixed direction ([1.0, 0.0, ...])."""

    def __init__(self, settings) -> None:
        self._dim = settings.embedding_vector_dim

    def _vector(self) -> list[float]:
        return [1.0] + [0.0] * (self._dim - 1)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector() for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector()


class _TwoDirEmbedder:
    """Offline stub: passages mentioning 'first' get one direction, others another.

    Query embeds identically to the 'first' direction, so the dense leg (and
    therefore RRF, since the lexical leg finds nothing — no ``passages_fts`` rows
    exist for versions saved via ``versions.save`` directly) ranks the 'first'
    passage ahead of the 'second' one, deterministically.
    """

    def __init__(self, settings) -> None:
        self._dim = settings.embedding_vector_dim

    def _vector(self, first: bool) -> list[float]:
        v = [0.0] * self._dim
        v[0 if first else 1] = 1.0
        return v

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector("first" in text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(first=True)


def test_retrieve_wires_rerank_and_reorders_by_cross_encoder_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli._retrieve calls rerank() between RRF and expand_parents (lode-vtf).

    Two notes are indexed through the dense leg with orthogonal vectors so RRF
    ranks the 'first' passage ahead of the 'second' one by construction (see
    ``_TwoDirEmbedder``). A stubbed cross-encoder — injected via
    ``lode.retrieval.FastEmbedCrossEncoder``, the seam ``rerank()`` falls back to
    when no scorer is passed — scores the 'second' passage higher. Asserting the
    live ``_retrieve`` path (not just ``lode.retrieval``'s own unit tests) returns
    'second' ahead of 'first' proves rerank actually fires from cli.py.
    """
    settings = load_settings(embedding_vector_dim=2)
    db_path = tmp_path / "lode.db"
    lance_dir = config.lance_dir(db_path)
    conn = init_db(db_path)
    try:
        v_first = save(
            conn, "n-first", "first passage body", settings=settings
        ).version_id
        v_second = save(
            conn, "n-second", "second passage body", settings=settings
        ).version_id

        embedder = _TwoDirEmbedder(settings)
        for version in (v_first, v_second):
            assert (
                embed(
                    conn,
                    version,
                    lance_dir=lance_dir,
                    embedder=embedder,
                    settings=settings,
                )
                > 0
            )

        class _InvertingCrossEncoder:
            def __init__(self, settings: object) -> None:
                pass

            def rerank(self, query: str, documents: list[str]) -> list[float]:
                # Prefer whichever document mentions 'second' -- the opposite of
                # the dense-leg/RRF order established above.
                return [1.0 if "second" in doc else 0.0 for doc in documents]

        monkeypatch.setattr(
            "lode.retrieval.FastEmbedCrossEncoder", _InvertingCrossEncoder
        )

        # Lexically disjoint from both bodies: no passages_fts rows exist anyway
        # (versions.save bypasses the LexicalCacheBackend), so only the dense leg
        # — and then rerank — determines the order.
        context = cli._retrieve(
            conn,
            "unrelated wording entirely",
            lance_dir=lance_dir,
            embedder=embedder,
            settings=settings,
        )
    finally:
        conn.close()

    assert [item.target_version for item in context] == [v_second, v_first]


def test_retrieve_respects_rerank_disabled_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings.rerank_enabled=False fully bypasses the rerank stage (lode-vtf).

    Mirrors ``lode.retrieval.rerank``'s own "fully bypassed" contract, but proves
    it holds through the live ``_retrieve`` path: the cross-encoder class is
    stubbed to explode if constructed, and the dense-leg order established by
    ``_TwoDirEmbedder`` (first ahead of second) survives unchanged.
    """

    class _ExplodingCrossEncoder:
        def __init__(self, settings: object) -> None:
            raise AssertionError(
                "cross-encoder must not be constructed when rerank_enabled=False"
            )

    monkeypatch.setattr("lode.retrieval.FastEmbedCrossEncoder", _ExplodingCrossEncoder)

    settings = load_settings(embedding_vector_dim=2, rerank_enabled=False)
    db_path = tmp_path / "lode.db"
    lance_dir = config.lance_dir(db_path)
    conn = init_db(db_path)
    try:
        v_first = save(
            conn, "n-first", "first passage body", settings=settings
        ).version_id
        v_second = save(
            conn, "n-second", "second passage body", settings=settings
        ).version_id

        embedder = _TwoDirEmbedder(settings)
        for version in (v_first, v_second):
            assert (
                embed(
                    conn,
                    version,
                    lance_dir=lance_dir,
                    embedder=embedder,
                    settings=settings,
                )
                > 0
            )

        context = cli._retrieve(
            conn,
            "unrelated wording entirely",
            lance_dir=lance_dir,
            embedder=embedder,
            settings=settings,
        )
    finally:
        conn.close()

    assert [item.target_version for item in context] == [v_first, v_second]


def test_retrieve_wires_graph_expand_and_includes_linked_note(
    tmp_path: Path,
) -> None:
    """cli._retrieve calls graph_expand() after expand_parents (lode-vtf).

    note-a is the only note directly retrievable (dense leg); note-b is reachable
    only via an AI-inferred edge from note-a and is deliberately never indexed
    into LanceDB, so the only way its passage can reach the Q&A context is
    through graph_expand's edge traversal — proving the live ``_retrieve`` path
    wires it in, not just ``lode.retrieval``'s own unit tests. rerank is disabled
    here to keep this test focused on graph_expand (rerank's own wiring is
    covered by the dedicated rerank tests above) and offline (no model load).
    """
    settings = load_settings(embedding_vector_dim=2, rerank_enabled=False)
    db_path = tmp_path / "lode.db"
    lance_dir = config.lance_dir(db_path)
    conn = init_db(db_path)
    try:
        v_a = save(conn, "note-a", "seed note body", settings=settings).version_id
        v_b = save(conn, "note-b", "linked note body", settings=settings).version_id

        embedder = _OneDirEmbedder(settings)
        # Only note-a is indexed into the dense store; note-b's passage exists
        # only in SQLite (never embedded), so it can never surface as a direct
        # hit -- graph_expand is the only path it can reach the context through.
        assert (
            embed(conn, v_a, lance_dir=lance_dir, embedder=embedder, settings=settings)
            > 0
        )
        conn.execute(
            "INSERT INTO passages "
            "(passage_id, target_version, ord, char_range, text, parent_block) "
            "VALUES ('p-b', ?, 0, '0:16', 'linked note body', 'linked note body')",
            (v_b,),
        )

        note_a_id = conn.execute(
            "SELECT note_id FROM versions WHERE version_id = ?", (v_a,)
        ).fetchone()[0]
        note_b_id = conn.execute(
            "SELECT note_id FROM versions WHERE version_id = ?", (v_b,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, status) "
            "VALUES (?, ?, 'ai', 'fresh')",
            (note_a_id, note_b_id),
        )
        conn.commit()

        context = cli._retrieve(
            conn,
            "unrelated wording entirely",
            lance_dir=lance_dir,
            embedder=embedder,
            settings=settings,
        )
    finally:
        conn.close()

    target_versions = {item.target_version for item in context}
    assert v_a in target_versions
    assert v_b in target_versions  # only reachable via graph_expand


def test_ask_requires_a_question() -> None:
    result = runner.invoke(app, ["ask"])
    assert result.exit_code != 0  # missing required argument


@pytest.mark.slow
def test_ask_cli_threads_settings_to_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured entailment_threshold reaches the gate via 'lode ask' (lode-xdr).

    Mirrors test_ask_honors_configured_entailment_threshold (test_cited_answer.py)
    but exercises the CLI entry point rather than cited_answer.ask directly, proving
    that cli.ask constructs and threads a Settings through to the faithfulness gate.

    A synthesis claim (span verbatim-present but not extractively coupled, so it
    reaches step 3) scoring 0.5 from the stub scorer survives under a lax threshold
    and is dropped under a strict one — proving the threaded Settings, not a buried
    Settings() default, decides the gate outcome.
    """
    db_path = tmp_path / "lode.db"
    # Span "rerank OFF" is verbatim in the body; claim text "rerank is on" is not
    # extractively coupled with it (payload {rerank, on} is not a subset of span
    # tokens {rerank, off}), so the claim reaches NLI step 3.
    body = "lode ships rerank OFF in the walking skeleton; deepen it later."
    _seed_corpus(db_path, note_id="n1", version_id="v1", body=body, passage_id="p1")
    _offline_embedder(monkeypatch)
    _mock_qa(
        monkeypatch,
        [
            Claim(
                text="rerank is on",
                support=[Support(version_id="v1", quoted_span="rerank OFF")],
            )
        ],
    )

    # Stub the NLI scorer so step 3 stays offline and returns a fixed score (0.5).
    # Matches FastEmbedEntailmentScorer's __init__ signature: takes settings.
    class _ConstantScorer:
        def __init__(self, settings: object) -> None:
            pass

        def entailment(self, premise: str, hypothesis: str) -> float:
            return 0.5

    monkeypatch.setattr("lode.gate.FastEmbedEntailmentScorer", _ConstantScorer)

    # Use a question containing "rerank" so the FTS leg surfaces the seeded passage
    # and bodies is populated from the store — the verbatim-span check can then run.
    question = "rerank behavior"

    # Strict threshold (0.8 > 0.5): the configured Settings is threaded to the gate,
    # which drops the claim → abstain. Patches lode.cli.load_settings (lode-40g:
    # `ask` now resolves settings via load_settings(), not a bare Settings()).
    monkeypatch.setattr(
        "lode.cli.load_settings", lambda: load_settings(entailment_threshold=0.8)
    )
    strict = runner.invoke(app, ["ask", question, "--db", str(db_path)])
    assert strict.exit_code == 0
    assert cli._ABSTAIN_LINE in strict.stdout

    # Lax threshold (0.4 < 0.5): same claim, same scorer, the gate survives it.
    monkeypatch.setattr(
        "lode.cli.load_settings", lambda: load_settings(entailment_threshold=0.4)
    )
    lax = runner.invoke(app, ["ask", question, "--db", str(db_path)])
    assert lax.exit_code == 0
    assert "rerank is on" in lax.stdout


def test_format_cited_answer_renders_claim_with_citation() -> None:
    answer = CitedAnswer(
        claims=(
            Claim(
                text="lode is append-only.",
                support=[Support(version_id="v9", quoted_span="append-only")],
            ),
        ),
        withheld_citations=(),
    )

    lines = cli._format_cited_answer(answer, {"v9": "2026-06-18T00:00:00.000Z"})

    assert lines[0] == "lode is append-only."
    assert "version_id v9" in lines[1]
    assert "as of 2026-06-18T00:00:00.000Z" in lines[1]
    assert '"append-only"' in lines[1]


def test_format_cited_answer_renders_snapshot_citation_with_fetched_at() -> None:
    """Acceptance (lode-w0h.4): an external citation's line carries its fetched_at."""
    answer = CitedAnswer(
        claims=(
            Claim(
                text="rotate the certs.",
                support=[Support(snapshot_id="s3", quoted_span="rotate the certs")],
            ),
        ),
        withheld_citations=(),
    )

    lines = cli._format_cited_answer(answer, {"s3": "2026-06-01T00:00:00.000Z"})

    assert "snapshot_id s3" in lines[1]
    assert "as of 2026-06-01T00:00:00.000Z" in lines[1]


def test_format_citation_marks_an_unresolved_target_as_of_unknown() -> None:
    """Practically unreachable (the gate already verified the body exists), but
    rendered honestly rather than assumed away — mirrors ``lode.tui.ask``."""
    line = cli._format_citation(Support(version_id="missing", quoted_span="x"), None)

    assert "as of unknown" in line


def test_format_cited_answer_surfaces_withheld_even_on_abstention() -> None:
    answer = CitedAnswer(claims=(), withheld_citations=(WithheldCitation("v-secret"),))

    lines = cli._format_cited_answer(answer, {})

    assert lines[0] == cli._ABSTAIN_LINE
    assert any("v-secret" in line and "withheld" in line for line in lines)


# --- lode config (resolved paths read-out, lode-ftc) ------------------------


def test_config_surfaces_every_resolved_path_under_lode_home(tmp_path: Path) -> None:
    # Acceptance: $LODE_HOME root, DB, vector store, log dir, and config file path
    # are all displayed, resolved under the single root (docs/configuration.md).
    home = tmp_path / "home"
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    out = result.stdout
    assert str(home) in out  # the resolved root
    assert str(home / "lode.db") in out
    assert str(home / "lode.db.lock") in out
    assert str(home / "lancedb") in out
    assert str(home / "logs") in out
    assert str(home / "config.toml") in out


def test_config_reports_config_file_present_or_absent(tmp_path: Path) -> None:
    # The optional config.toml is shown absent by default, present once it exists.
    home = tmp_path / "home"
    home.mkdir()
    absent = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert absent.exit_code == 0
    assert "config.toml  (absent)" in absent.stdout

    (home / "config.toml").write_text("", encoding="utf-8")
    present = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert "config.toml  (present)" in present.stdout


def test_config_flags_env_override_vs_default(tmp_path: Path) -> None:
    # The effective source of the root is surfaced: env override when set, else
    # the ~/.lode default (docs design: "show the effective env-var override").
    home = tmp_path / "home"
    overridden = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert "($LODE_HOME)" in overridden.stdout

    default = runner.invoke(app, ["config"], env={"LODE_HOME": ""})
    assert "(default)" in default.stdout


def test_config_db_override_shifts_displayed_db_and_vector_store(
    tmp_path: Path,
) -> None:
    # A per-invocation --db override moves the displayed DB, its lock, and the
    # co-located vector store; the root/logs/config still come from $LODE_HOME.
    home = tmp_path / "home"
    custom_db = tmp_path / "elsewhere" / "custom.db"
    result = runner.invoke(
        app, ["config", "--db", str(custom_db)], env={"LODE_HOME": str(home)}
    )
    assert result.exit_code == 0
    out = result.stdout
    assert str(custom_db) in out
    assert str(tmp_path / "elsewhere" / "custom.db.lock") in out
    assert str(tmp_path / "elsewhere" / "lancedb") in out
    # logs and config stay under the root, not beside the overridden DB.
    assert str(home / "logs") in out
    assert str(home / "config.toml") in out


# --- lode work (async worker drain, lode-i05.3) ----------------------------


def _noop_embed_registry() -> dict:
    """A stub registry with a no-op embed handler for offline CLI tests."""
    return {"embed": lambda conn, tv, db, s: None}


def test_work_drains_pending_embed_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' drains all ready pending embed jobs and exits 0.

    Acceptance: a claimed embed job runs once and lands (status='done');
    'lode work' drains all runnable jobs then exits.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            # Insert three embed jobs directly (no version row needed for noop handler).
            for i in range(3):
                conn.execute(
                    "INSERT INTO jobs (type, target_version) VALUES (?, ?)",
                    ("embed", f"ver-{i}"),
                )
    finally:
        conn.close()

    # Patch the module-level registry so the handler runs offline (no model).
    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "drained 3 job(s)" in result.stdout

    # All embed jobs are done.
    reader = sqlite3.connect(db_path)
    try:
        statuses = {
            r[0]
            for r in reader.execute(
                "SELECT status FROM jobs WHERE type = 'embed'"
            ).fetchall()
        }
    finally:
        reader.close()
    assert statuses == {"done"}


def _embed_outcome_registry() -> dict:
    """A stub registry whose embed handler mimics the real one's outcome return
    (lode-1gr.4) -- a one-line human-readable summary of what it produced.
    """
    return {"embed": lambda conn, tv, db, s: f"embedded {tv}: 3 passages"}


def test_work_prints_per_job_embed_outcome_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' echoes a per-job outcome line ahead of 'drained N job(s)' (lode-1gr.4).

    Acceptance: a drain pass that runs embed jobs prints a per-note
    passage-count line.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version) VALUES (?, ?)",
                ("embed", "ver-1"),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _embed_outcome_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "embedded ver-1: 3 passages" in result.stdout
    # Outcome line precedes the existing job-count summary.
    outcome_idx = result.stdout.index("embedded ver-1: 3 passages")
    drained_idx = result.stdout.index("drained 1 job(s)")
    assert outcome_idx < drained_idx


def test_work_never_dead_letters_enrich(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' must never dead-letter an enrich job (lode-npx.2 batch path).

    After lode-npx.2 the batch pre-step handles pending enrich jobs via the
    Batches API.  When the version being enriched is not found in the DB (a
    synthetic test case), submit_enrich_batch marks the job 'done' immediately
    (same skip logic as enrich_version).  The job must never become 'dead'.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            enqueue_derive_jobs(conn, "ver-1")  # embed + enrich (no version row)
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0

    reader = sqlite3.connect(db_path)
    try:
        (enrich_status,) = reader.execute(
            "SELECT status FROM jobs WHERE type = 'enrich'"
        ).fetchone()
    finally:
        reader.close()
    # The version is absent → submit_enrich_batch marks the job 'done' (skip path).
    # It must never be 'dead' (dead-lettered).
    assert enrich_status != "dead"


def test_work_wait_exits_zero_once_queue_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work --wait' returns once the queue fully drains, no timeout needed.

    Acceptance: with only embed jobs (which the noop handler clears in the
    first drain pass), --wait sees an empty pending/running set immediately
    and exits 0 without ever checking the timeout.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version) VALUES (?, ?)",
                ("embed", "ver-0"),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path), "--wait"])
    assert result.exit_code == 0, result.output
    assert "drained 1 job(s)" in result.stdout


def test_work_wait_times_out_naming_outstanding_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work --wait' exits non-zero and names jobs still in flight at timeout.

    A 'refresh' job has no registered handler (registry patched to embed-only,
    same as the other offline CLI tests) so it can never be claimed and stays
    'pending' forever -- simulating a queue that never fully drains. The clock
    is faked past the deadline on the first check so the test doesn't actually
    block for Settings.work_wait_timeout_s.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version) VALUES (?, ?)",
                ("refresh", "ver-stuck"),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    # The FIRST call to time.monotonic() establishes the deadline (work()'s
    # `deadline = time.monotonic() + timeout_s`); every call AFTER that must
    # read as far in the future so the loop's first timeout check trips
    # immediately. A constant fake clock would be wrong here: both the
    # deadline calc and the check would read the identical value, so
    # `now >= deadline` (now == deadline - timeout_s) would never hold and
    # the loop would spin for real (sleeping --interval seconds) forever.
    calls = {"n": 0}

    def _fake_monotonic() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1_000_000.0

    monkeypatch.setattr(cli.time, "monotonic", _fake_monotonic)
    # Belt-and-suspenders: never really sleep in this test even if the
    # timeout check above didn't fire on the first pass.
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    result = runner.invoke(app, ["work", "--db", str(db_path), "--wait"])
    assert result.exit_code == 1
    assert "timed out" in result.stderr
    assert "refresh" in result.stderr
    assert "pending" in result.stderr


def test_work_wait_rejects_loop_combo(tmp_path: Path) -> None:
    """'--wait' and '--loop' specify contradictory exit conditions -- refused."""
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["work", "--db", str(db_path), "--wait", "--loop"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stderr


def test_work_without_wait_is_unchanged_one_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --wait, behaviour is the pre-existing one-shot drain (no polling)."""
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version) VALUES (?, ?)",
                ("refresh", "ver-stuck"),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "drained 0 job(s)" in result.stdout


def test_work_refuses_when_lock_held(tmp_path: Path) -> None:
    """A second 'lode work' must refuse when the lockfile holds a live PID.

    Acceptance: the loop runs under the advisory lock (a second 'lode work'
    refuses).
    """
    import os

    from lode.lock import lock_path

    db_path = tmp_path / "lode.db"
    # Write the current process's PID into the lockfile — we are live.
    lf = lock_path(db_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(os.getpid()))

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "lode worker" in result.stderr or "pid" in result.stderr


# --- lode work honors a config.toml override end-to-end (lode-40g) ---------


def _stale_iso(seconds_ago: int) -> str:
    """An ISO-8601 timestamp ``seconds_ago`` seconds in the past."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _seed_external_snapshot_120s_old(db_path: Path) -> None:
    """Seed one external + head snapshot, 120s stale, with embed/enrich already
    'done' so the default embed_gap/enrich_gap reconcile steps (also run by
    'lode work') have nothing to re-enqueue -- isolates the test below to the
    one step (refresh_stale) that refresh_ttl_s actually gates, and keeps it
    fully offline (no fastembed/Anthropic call from the drain loop).

    120s is well within the default refresh_ttl_s (3600s, so the un-configured
    default would NOT flag it) but past a 60s config-file override.
    """
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
                ("ext-1",),
            )
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status, fetched_at) "
                "VALUES (?, ?, ?, 'ok', ?)",
                ("snap-1", "ext-1", "body text", _stale_iso(120)),
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = 'snap-1' "
                "WHERE external_id = 'ext-1'"
            )
            conn.execute(
                "INSERT INTO jobs (type, target_version, status) "
                "VALUES ('embed', 'snap-1', 'done')"
            )
            conn.execute(
                "INSERT INTO jobs (type, target_version, status) "
                "VALUES ('enrich', 'snap-1', 'done')"
            )
    finally:
        conn.close()


def _refresh_job_statuses(db_path: Path, external_id: str = "ext-1") -> list[str]:
    reader = sqlite3.connect(db_path)
    try:
        return [
            r[0]
            for r in reader.execute(
                "SELECT status FROM jobs WHERE type = 'refresh' AND target_version = ?",
                (external_id,),
            ).fetchall()
        ]
    finally:
        reader.close()


def test_work_honors_config_file_refresh_ttl_s_end_to_end(tmp_path: Path) -> None:
    """A config.toml override of refresh_ttl_s actually reaches 'lode work' (lode-40g).

    Regression coverage for load_settings() having zero production callers: this
    proves the override changes *observable behavior* end-to-end, not just that
    Settings parses it. Companion
    test_work_uses_default_refresh_ttl_s_without_a_config_file shows the same
    120s-old snapshot is NOT flagged without the file present -- so this can
    only be the file value reaching reconcile()'s refresh_stale step via
    'lode work' -> load_settings() -> reconcile(conn, settings) (lode-09n).
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("refresh_ttl_s = 60\n", encoding="utf-8")

    db_path = tmp_path / "lode.db"
    _seed_external_snapshot_120s_old(db_path)

    result = runner.invoke(
        app, ["work", "--db", str(db_path)], env={"LODE_HOME": str(home)}
    )
    assert result.exit_code == 0, result.output
    # One refresh job row exists for ext-1 -- reconcile()'s refresh_stale step
    # enqueued it. Its *terminal* status (pending/failed/...) is
    # _refresh_handler's own concern (it genuinely tries to fetch "ext-1" as a
    # URL and fails, harmlessly, with a retry backoff) -- irrelevant to what
    # this test is proving: that refresh_ttl_s from the file is what caused the
    # row to be enqueued in the first place.
    assert len(_refresh_job_statuses(db_path)) == 1


def test_add_honors_config_file_redaction_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.toml redact_before_index_patterns override reaches `lode add` (lode-40g).

    The sharp edge of this ticket's bug class, and the reason `add` resolves
    settings once and threads them into repo.save(): save() runs
    redact_before_index() off the settings it is handed, so while `add` passed
    none, a secret pattern the user had configured was silently ignored and the
    raw secret was written to the passages/FTS index. `ACME-` is not in the
    shipped seed set (_SECRET_SEED_PATTERNS), so nothing but the config file
    can cause it to be redacted here.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'redact_before_index_patterns = ["ACME-[0-9]+"]\n', encoding="utf-8"
    )
    # `add` runs the enrich leg inline; keep it offline.
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", lambda *a, **kw: None)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(
        app,
        ["add", "--db", str(db_path), "the token is ACME-12345 keep it safe"],
        env={"LODE_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output

    reader = sqlite3.connect(db_path)
    try:
        passages = " ".join(
            r[0] for r in reader.execute("SELECT text FROM passages").fetchall()
        )
    finally:
        reader.close()

    assert passages, "expected lode add to write at least one passage"
    assert "ACME-12345" not in passages, (
        "the configured redaction pattern did not reach repo.save() — a secret "
        "the user configured was indexed in the clear"
    )
    assert REDACTION_MARKER in passages


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("refresh_ttl_s =\n", "TOML syntax error"),
        ("not_a_real_knob = 1\n", "unknown key (pydantic extra=forbid)"),
        ("refresh_ttl_s = 0\n", "out-of-range value (field validator)"),
    ],
)
def test_cli_reports_a_bad_config_file_without_a_traceback(
    tmp_path: Path, body: str, kind: str
) -> None:
    """A typo in the hand-edited config.toml is a clean CLI error, not a crash.

    config.toml only started being *read* in lode-40g, which introduced this
    failure mode: an unusable file made load_settings() raise straight through
    every entry point, dumping a Python traceback at the terminal. cli's
    _resolve_settings() converts both failure kinds (TOMLDecodeError and
    pydantic ValidationError -- see tests/test_config.py) into the one-line
    stderr + exit-1 convention every other user-facing CLI failure uses.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(body, encoding="utf-8")

    result = runner.invoke(
        app,
        ["work", "--db", str(tmp_path / "lode.db")],
        env={"LODE_HOME": str(home)},
    )
    assert result.exit_code == 1, f"{kind}: {result.output}"
    assert "invalid config file" in result.stderr, kind
    assert str(home / "config.toml") in result.stderr, kind
    # The load-bearing assertion. CliRunner also reports exit_code 1 for an
    # *unhandled* exception, so exit_code alone cannot tell a clean error from
    # a crash: what proves _resolve_settings() caught it is that the raised
    # error is typer's own Exit (SystemExit) rather than the TOMLDecodeError /
    # ValidationError that would otherwise have escaped to the terminal.
    assert isinstance(result.exception, SystemExit), f"{kind}: {result.exception!r}"


def test_work_uses_default_refresh_ttl_s_without_a_config_file(
    tmp_path: Path,
) -> None:
    """Control for the test above: with no config.toml, the default
    refresh_ttl_s (3600s) does NOT flag the same 120s-old snapshot -- isolating
    that the config file's value, not some other change, causes the refresh job
    to appear.
    """
    home = tmp_path / "home"
    home.mkdir()  # no config.toml written -- every knob stays at its default

    db_path = tmp_path / "lode.db"
    _seed_external_snapshot_120s_old(db_path)

    result = runner.invoke(
        app, ["work", "--db", str(db_path)], env={"LODE_HOME": str(home)}
    )
    assert result.exit_code == 0, result.output
    assert _refresh_job_statuses(db_path) == []

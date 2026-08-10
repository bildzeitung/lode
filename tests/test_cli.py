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
Anthropic client mocked so the gate runs offline); ``no-egress`` (lode-w0h.7:
the no-egress-tier control surface for an external source -- sets/``--clear``s
``externals.no_egress`` via ``lode.externals.set_no_egress``, refusing an unknown
external_id); and ``dump-html`` (spec 06 item 7c, lode-olmi.7: prints a note's
drawn-down external's raw fetched HTML, ``snapshots.raw_payload`` -- resolving
the note the same way ``show``/``purge`` do, disambiguating a multi-external
note by listing or by a 1-based-index/id selector, and reporting a
tombstone/no-HTML snapshot cleanly rather than dumping empty).
"""

import io
import itertools
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
import typer.main
from rich.console import Console
from rich.table import Table
from typer.testing import CliRunner

from lode import __version__, cli, config, retrieval
from lode.answer import Claim, Support
from lode.auth import AuthError
from lode.cited_answer import CitedAnswer
from lode.cli import app
from lode.config import Settings, load_settings
from lode.egress import WithheldCitation
from lode.embedding import embed
from lode.externals import ingest_snapshot
from lode.hashing import NO_PARENT, content_version_id
from lode.ids import short_version_id
from lode.jobs import enqueue_derive_jobs, now_iso
from lode.llm_provider import AnthropicProvider, LLMAuthError, LLMProviderError
from lode.redact import REDACTION_MARKER
from lode.storage import init_db
from lode.versions import delete, purge, save

runner = CliRunner()

# Every subcommand is real: `add` (lode-y42.1), `ask` (lode-y42.2), `status` /
# `jobs` (lode-y42.3), `egress` (lode-fk8.3), `purge` (lode-7cx), `notes` (lode-1gr.1),
# `config` (lode-ftc), `work` (lode-i05.3: async work queue drain), `models`
# (lode-og3: the `models pull` sub-app group, explicit model-weights prefetch).
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
    "dump-html",
    "config",
    "work",
    "tui",
    "models",
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


#: The exact order ``lode --help`` lists its subcommands in. Typer/click render
#: the command table in REGISTRATION order (groups, i.e. ``models``, after the
#: plain commands), so this is a user-visible output contract, not an internal
#: detail -- and since the lode-35nu.9 split it is produced by
#: ``lode.cli._COMMAND_MODULES``' declared order rather than by one file's
#: top-to-bottom layout. Pinned here because that split silently reordered it
#: once already: an alphabetised import block, plus modules pulled in early and
#: transitively by a sibling, made the real order an accident of import
#: statements. Reordering the help table is a deliberate UX change -- update
#: this list in the same commit, never to make a red test green.
HELP_COMMAND_ORDER = [
    "add",
    "ask",
    "purge",
    "recover",
    "notes",
    "show",
    "status",
    "reembed",
    "reindex-lexical",
    "reenrich",
    "jobs",
    "egress",
    "no-egress",
    "dump-html",
    "config",
    "verify",
    "tui",
    "version",
    "work",
    "backfill",
    "models",
]


def test_help_lists_subcommands_in_the_pinned_order() -> None:
    # `typer.main.get_command` returns a TyperGroup -- a click Group subclass
    # whose `list_commands` deliberately does NOT sort (click's own base does),
    # which is exactly what makes registration order the rendered order.
    group = typer.main.get_command(app)
    assert group.list_commands(click.Context(group)) == HELP_COMMAND_ORDER


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


@contextmanager
def _local_tz(tz: str) -> Iterator[None]:
    """Pin the process's local timezone (``time.tzset``, POSIX-only) for a block.

    ``_short_date`` converts a stored UTC timestamp to system local time
    before formatting (lode-olmi.5); pinning ``TZ`` here makes that
    conversion deterministic regardless of the host machine's real
    timezone, and restores whatever ``TZ`` was set to (or unsets it) on
    exit so it can never leak into a later test.
    """
    original = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def _noop_enrich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `add`'s immediate-enrich fast path for tests that don't care about
    enrichment's outcome (lode-85q).

    ``add`` runs the enrich leg inline (lode-npx.2): without this, a test that
    just invokes ``add`` and asserts on unrelated state (a note's body, a log
    directory, a purge/recover flow, …) reaches the *real*, un-mocked
    ``enrich_version`` -> ``anthropic.Anthropic`` path (tests/conftest.py's
    autouse guard now fails loudly on exactly that, closing the gap lode-8xg
    found). Same pattern as ``test_notes_lists_the_full_id_date_and_summary``'s
    inline stub above and ``test_add_calls_enrich_immediately`` elsewhere in
    this file — patch at the real lookup site (``lode.enrich``), not
    ``lode.cli`` (which has no ``enrich_version`` symbol of its own).
    """
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", lambda *a, **k: None)


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


def test_add_auth_error_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanent, missing-credentials failure must not crash capture either
    (lode-9yy): 'lode add' stays instant regardless of credential state.

    Unlike a transient enrich failure (test_add_enrich_failure_is_non_fatal,
    which lands the job 'failed' with attempts=1), an AuthError is reset
    straight back to 'pending' uncharged by run_one and silently swallowed by
    _enrich_immediately -- the note still saves and 'lode add' still exits 0,
    with no actionable-message noise on every single capture. The next
    explicit 'lode work' is what reports it loudly.
    """
    import lode.enrich as enrich_mod

    def _no_credentials(conn, version_id, settings, *, client=None):
        raise AuthError("no credentials (test)")

    monkeypatch.setattr(enrich_mod, "enrich_version", _no_credentials)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "note body", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    note_id = result.stdout.strip()
    assert note_id

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
    # Uncharged and left claimable, unlike a transient failure's ("failed", 1).
    assert jobs_by_type["enrich"] == ("pending", 0)


def test_add_llm_provider_error_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-auth 'LLMProviderError' must not crash capture either (lode-s08c).

    'LLMProviderError' and 'AuthError' are SIBLING RuntimeError subclasses --
    neither is an ancestor of the other -- so the 'except AuthError' that
    already protected 'lode add' against a missing-credentials failure
    (test_add_auth_error_is_non_fatal) let this one through as a raw
    traceback, exactly the gap lode-yx1c already fixed for 'ask'/'work'.

    ``claim_and_run_one`` (as imported into ``lode.cli`` at call time) is
    stubbed directly, isolating ``_enrich_immediately``'s own handler --
    mirroring how the 'work' tests stub ``lode.worker.drain`` directly
    (test_work_exits_nonzero_with_actionable_message_on_llm_provider_error).
    Going through a real ``enrich_version`` stub would not exercise this
    path at all: ``run_one`` only re-raises ``(AuthError, LLMAuthError)`` --
    a *plain* non-auth ``LLMProviderError`` raised by a job handler is
    already absorbed as a transient failure before it ever reaches
    ``_enrich_immediately`` (``docs/storage.md`` "Transient vs. permanent
    job failures").
    """
    import lode.worker as worker_mod

    def _provider_error(conn, db_path, settings, *, types, target_version=None):
        raise LLMProviderError("provider returned 500", provider="anthropic")

    monkeypatch.setattr(worker_mod, "claim_and_run_one", _provider_error)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "note body", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    note_id = result.stdout.strip()
    assert note_id


def test_add_llm_auth_error_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMAuthError (a non-Anthropic missing-credential) is non-fatal too (lode-s08c).

    'LLMAuthError' subclasses 'LLMProviderError', not 'AuthError', so the old
    bare 'except AuthError' let it out of 'lode add' as a raw traceback. This
    is the case the widening actually fixes, and unlike the plain
    'LLMProviderError' above it IS reachable end to end -- run_one re-raises
    '(AuthError, LLMAuthError)'. So it is driven the same way its AuthError
    twin is (test_add_auth_error_is_non_fatal): stub the real 'enrich_version'
    handler and let the genuine run_one / claim_and_run_one path carry it up,
    rather than stubbing claim_and_run_one itself. That also lets the same
    uncharged-reset assertions apply.
    """
    import lode.enrich as enrich_mod

    def _no_credentials(conn, version_id, settings, *, client=None):
        raise LLMAuthError("no OpenAI/Azure credentials (test)", provider="openai")

    monkeypatch.setattr(enrich_mod, "enrich_version", _no_credentials)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "note body", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    note_id = result.stdout.strip()
    assert note_id

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
    # Uncharged and left claimable, unlike a transient failure's ("failed", 1).
    assert jobs_by_type["enrich"] == ("pending", 0)


def test_add_reads_body_from_stdin_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "--db", str(db_path)], input="from stdin\n")
    assert result.exit_code == 0
    note_id = result.stdout.strip()
    # Stored verbatim — the trailing newline is preserved, not stripped.
    assert _rows(
        db_path, "SELECT body FROM versions WHERE note_id = ?", (note_id,)
    ) == [("from stdin\n",)]


def test_add_uses_lode_home_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # $LODE_HOME is the single root: with no --db, `add` writes $LODE_HOME/lode.db
    # (lode-qd9, replacing the old $LODE_DB binding).
    _noop_enrich(monkeypatch)
    home = tmp_path / "home"
    result = runner.invoke(app, ["add", "via env"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    assert (home / "lode.db").exists()


def test_add_logs_land_under_lode_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance: logs land in $LODE_HOME/logs/ (lode-qd9). The group callback
    # attaches a file handler there on every command.
    _noop_enrich(monkeypatch)
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


def test_add_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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
            due = now_iso()
            conn.executemany(
                "INSERT INTO jobs "
                "(type, target_version, status, attempts, last_error, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("embed", "ver-aaaaaaaaaaaaaaaa", "pending", 0, None, due),
                    ("enrich", "ver-aaaaaaaaaaaaaaaa", "running", 1, None, due),
                    ("embed", "ver-bbbbbbbbbbbbbbbb", "done", 1, None, due),
                    (
                        "enrich",
                        "ver-bbbbbbbbbbbbbbbb",
                        "dead",
                        3,
                        "RateLimitError",
                        due,
                    ),
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


@pytest.fixture
def warm_model_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `lode status`'s cold-cache probe to "warm", deterministically.

    The probe answers from the REAL machine-level weights cache: the autouse
    `_isolate_lode_home` fixture symlinks $LODE_HOME/models at
    `model_cache_dir()` and only `mkdir(exist_ok=True)`s it, so on a machine
    that has never run `lode models pull` -- a fresh clone, i.e. exactly the
    CLAUDE.md "New machine setup" path, and any CI runner -- that directory is
    EMPTY and every resolved model probes cold. A test that asserts the
    all-clear footer while depending on that ambient state is green only where
    the weights happen to be present; it fails on the machines least able to
    explain why (verified: the three tests using this fixture all failed under
    a $LODE_HOME with no weights before it existed).

    Stubbing the probe here is not a coverage loss: the probe's real cold path
    is exercised end-to-end by `test_status_hints_cold_model_cache` (a genuinely
    empty $LODE_HOME), and its resolution logic unit-tested in
    `test_model_cache_probe_*` -- both hermetic. This fixture isolates the
    FOOTER's logic, which is what these three tests are actually about.
    """
    monkeypatch.setattr("lode.cli._cold_model_cache", lambda _settings: False)


def test_status_empty_db_reports_all_zero(
    tmp_path: Path, warm_model_cache: None
) -> None:
    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    # Job counts render as a rich Table (lode-l38d.6/.11) -- assert cell
    # content rather than the old single-line string, which no longer exists.
    for label, count in (
        ("Pending", "0"),
        ("Running", "0"),
        ("Done", "0"),
        ("Failed", "0"),
        ("Dead", "0"),
    ):
        row = next(ln for ln in result.stdout.splitlines() if label in ln)
        assert count in row
    assert "egress: 0 sends (none)" in result.stdout
    assert "dead-letters (dead jobs): 0" in result.stdout
    # All-clear footer: an explicit affirmative line, never silence
    # (lode-l38d.6's decision 3 -- an absent hint must not read as an
    # absent check). Warm cache pinned by the fixture, not by whatever the
    # machine happens to have cached -- see `warm_model_cache`.
    assert "No action needed." in result.stdout


def test_status_summarizes_jobs_egress_and_dead_letters(
    tmp_path: Path, warm_model_cache: None
) -> None:
    db_path = tmp_path / "lode.db"
    _seed_jobs(db_path)
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    # Seed has: 1 pending, 1 running, 1 done, 0 failed, 1 dead -- each count
    # is its own table cell (Status | Count columns).
    for label, count in (
        ("Pending", "1"),
        ("Running", "1"),
        ("Done", "1"),
        ("Failed", "0"),
        ("Dead", "1"),
    ):
        row = next(ln for ln in result.stdout.splitlines() if label in ln)
        assert count in row
    # Egress summary totals across purposes and breaks them out.
    assert "egress: 3 sends (enrich: 1, qa: 2)" in result.stdout
    # The single dead job surfaces as a dead-letter with its last error.
    assert "dead-letters (dead jobs): 1" in result.stdout
    assert "(enrich) target=ver-bbbbbbbb…: RateLimitError" in result.stdout
    # Action hint: 1 pending job -> a hint to drain the queue (lode-l38d.6).
    assert "run 'lode work' to drain the queue" in result.stdout
    # The single dead job is an 'enrich' (self-healing) -- lode-8vcq gives it
    # its own dead-letter hint too, distinct from (and in addition to) the
    # pending-job hint above; both are expected to fire together here.
    hint_lines = [ln for ln in result.stdout.splitlines() if "Action needed" in ln]
    assert any("self-healing" in ln for ln in hint_lines)


def _insert_dead_job(db_path: Path, job_type: str, target_version: str) -> None:
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs "
                "(type, target_version, status, attempts, last_error, next_attempt_at) "
                "VALUES (?, ?, 'dead', 3, 'boom', ?)",
                (job_type, target_version, now_iso()),
            )
    finally:
        conn.close()


def test_status_no_pending_hint_when_only_dead_letters_present(
    tmp_path: Path, warm_model_cache: None
) -> None:
    # Dead-letters alone (no pending/failed, warm cache) must not trip the
    # 'lode work' PENDING/FAILED hint -- that hint is specifically about jobs
    # 'lode work' can still retry, which a dead-lettered job is not
    # (lode-l38d.6). It's still expected to trip its own dead-letter hint
    # (lode-8vcq) -- that's covered by the arm-specific tests below, not here.
    db_path = tmp_path / "lode.db"
    _insert_dead_job(db_path, "enrich", "ver-cccccccccccccccc")
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "run 'lode work' to drain the queue" not in result.stdout


def test_status_hints_self_healing_dead_letters(
    tmp_path: Path, warm_model_cache: None
) -> None:
    # A dead-lettered embed/enrich job self-heals on the next reconciliation
    # scan -- the footer must say so and name 'lode work'/'lode
    # reembed'/'lode reenrich', not stay silent (lode-8vcq, acceptance #1).
    db_path = tmp_path / "lode.db"
    _insert_dead_job(db_path, "enrich", "ver-cccccccccccccccc")
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "self-healing" in result.stdout
    assert "'lode work'" in result.stdout
    assert "'lode reembed'" in result.stdout
    assert "'lode reenrich'" in result.stdout
    assert "tombstoned" not in result.stdout
    assert "No action needed." not in result.stdout


def test_status_hints_tombstoned_dead_letter(
    tmp_path: Path, warm_model_cache: None
) -> None:
    # A dead-lettered refresh job is a permanent tombstone, not self-healing
    # -- the footer must say so and point at re-adding the URL, distinct
    # from the self-healing hint (lode-8vcq, acceptance #2).
    db_path = tmp_path / "lode.db"
    _insert_dead_job(db_path, "refresh", "ver-dddddddddddddddd")
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "tombstoned" in result.stdout
    assert "re-add the URL" in result.stdout
    assert "self-healing" not in result.stdout
    assert "No action needed." not in result.stdout


def test_status_hints_both_self_healing_and_tombstoned_dead_letters(
    tmp_path: Path, warm_model_cache: None
) -> None:
    # Both hints fire together when both kinds of dead-letter are present
    # (lode-8vcq, acceptance #3).
    db_path = tmp_path / "lode.db"
    _insert_dead_job(db_path, "enrich", "ver-cccccccccccccccc")
    _insert_dead_job(db_path, "refresh", "ver-dddddddddddddddd")
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "self-healing" in result.stdout
    assert "tombstoned" in result.stdout


def test_status_hints_cold_model_cache(tmp_path: Path) -> None:
    # A fresh $LODE_HOME with no models/ dir at all -- every resolved model
    # is missing its cache subdir, so the probe must call this cold and hint
    # 'lode models pull' (lode-l38d.6's /challenge-decided cold definition:
    # ANY resolved model missing its cache counts, not a single dir-exists
    # stat). Overriding LODE_HOME here (rather than relying on the autouse
    # fixture's real-cache symlink) is the only way to exercise the cold path
    # deterministically.
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    home = tmp_path / "cold-home"
    result = runner.invoke(
        app, ["status", "--db", str(db_path)], env={"LODE_HOME": str(home)}
    )
    assert result.exit_code == 0
    assert "run 'lode models pull'" in result.stdout
    assert "No action needed." not in result.stdout


# --- lode-g274.4: model_revision manifest hints (mixed / drift, lode-crh8.1) ----


def _write_embedding_revision(
    db_path: Path, settings: Settings, *revisions: str | None
) -> None:
    """Seed the live LanceDB store with one vector row per ``revisions`` entry.

    Bypasses ``embed()`` entirely -- these tests are about ``lode status``'s
    footer reading the manifest back, not about the embed leg that writes it
    (covered by ``tests/test_embedding.py``).
    """
    from lode.vectorstore import VectorStore

    rows = [
        {
            "passage_id": f"p{i}",
            "target_version": "v1",
            "vector": [0.0] * settings.embedding_vector_dim,
            "model": settings.embedding_model,
            "model_revision": revision,
        }
        for i, revision in enumerate(revisions)
    ]
    VectorStore(config.lance_dir(db_path), settings).replace_vectors("v1", rows)


def test_status_hints_mixed_model_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_model_cache: None
) -> None:
    import huggingface_hub

    # The mixed check needs no live probe -- stub it offline so this test
    # stays hermetic and isolates just the mixed-index signal (drift is
    # covered by its own sibling test below).
    def _offline(repo_id: str, *, timeout: float) -> None:
        raise OSError("no network")

    monkeypatch.setattr(huggingface_hub, "model_info", _offline)

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    _write_embedding_revision(db_path, Settings(), "sha-1", "sha-2")

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "the index is mixed" in result.stdout
    assert "No action needed." not in result.stdout


def test_status_hints_model_revision_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_model_cache: None
) -> None:
    import huggingface_hub

    class _FakeModelInfo:
        sha = "sha-current"

    monkeypatch.setattr(
        huggingface_hub, "model_info", lambda repo_id, *, timeout: _FakeModelInfo()
    )

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    # Recorded revision disagrees with what a fresh probe resolves right now.
    _write_embedding_revision(db_path, Settings(), "sha-stale")

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "moved past the revision" in result.stdout
    assert "No action needed." not in result.stdout


def test_status_no_revision_hint_when_recorded_matches_live_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_model_cache: None
) -> None:
    import huggingface_hub

    class _FakeModelInfo:
        sha = "sha-current"

    monkeypatch.setattr(
        huggingface_hub, "model_info", lambda repo_id, *, timeout: _FakeModelInfo()
    )

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()
    _write_embedding_revision(db_path, Settings(), "sha-current")

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "Action needed" not in result.stdout
    assert "No action needed." in result.stdout


def test_status_no_revision_hint_when_never_embedded(
    tmp_path: Path, warm_model_cache: None
) -> None:
    # A fresh DB/store that has never embedded anything -- model_revisions()
    # returns empty, so neither hint can fire.
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "Action needed" not in result.stdout
    assert "No action needed." in result.stdout


def test_model_revision_status_is_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same non-fatal contract as _cold_model_cache: any internal failure must
    # answer (False, False), never raise -- lode status must never fail over
    # this hint.
    import lode.vectorstore as vectorstore_module

    class _BoomVectorStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("lancedb exploded")

    monkeypatch.setattr(vectorstore_module, "VectorStore", _BoomVectorStore)
    assert cli._model_revision_status(Settings(), "unused") == (False, False)


# --- lode-g274.7: `lode reembed` -- deliberate corpus regeneration ----------


def test_reembed_forces_fresh_job_past_a_done_embed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live head whose embed job already reached 'done' still gets a fresh one.

    This is the whole point of the command (lode-g274.7): 'done' is exactly
    what a stale model_revision looks like, so -- unlike the passive
    reconcile embed_gap step, which treats 'done' as covered -- reembed must
    force a new job regardless.
    """
    import lode.enrich as enrich_mod

    # 'add' opportunistically runs its own enrich job inline
    # (_enrich_immediately) -- stub it so this test, which only cares about
    # the embed leg, never constructs a real Anthropic client.
    monkeypatch.setattr(enrich_mod, "enrich_version", lambda conn, vid, s, **kw: None)

    db_path = tmp_path / "lode.db"
    result = runner.invoke(app, ["add", "hello world", "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(db_path)
    try:
        (version_id,) = conn.execute("SELECT head_version_id FROM notes").fetchone()
        # Simulate a completed initial embed (as if under a since-superseded
        # model revision) -- reconcile's own embed_gap step would treat this
        # as fully covered and enqueue nothing more.
        conn.execute("UPDATE jobs SET status = 'done' WHERE type = 'embed'")
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["reembed", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "enqueued 1 embed job(s)" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        statuses = [
            r[0]
            for r in reader.execute(
                "SELECT status FROM jobs WHERE type = 'embed' AND "
                "target_version = ? ORDER BY id",
                (version_id,),
            ).fetchall()
        ]
    finally:
        reader.close()
    # The original 'done' job is untouched; a fresh 'pending' one sits beside it.
    assert statuses == ["done", "pending"]


def test_reembed_no_live_heads_is_a_clean_no_op(tmp_path: Path) -> None:
    """An empty corpus enqueues nothing and says so, exiting 0."""
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["reembed", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no live heads to re-embed" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        (count,) = reader.execute("SELECT COUNT(*) FROM jobs").fetchone()
    finally:
        reader.close()
    assert count == 0


def test_reembed_covers_external_heads_too(tmp_path: Path) -> None:
    """An external's current head_snapshot_id is force-enqueued too (lode-621 shape).

    Mirrors live_head_versions' notes-UNION-externals scope -- reembed
    delegates to that same function rather than enumerating notes alone.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES ('ext-1', 'web')"
            )
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES ('snap-1', 'ext-1', 'body text', 'ok')"
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = 'snap-1' WHERE external_id = 'ext-1'"
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["reembed", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "enqueued 1 embed job(s)" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        statuses = [
            r[0]
            for r in reader.execute(
                "SELECT status FROM jobs WHERE type = 'embed' AND target_version = 'snap-1'"
            ).fetchall()
        ]
    finally:
        reader.close()
    assert statuses == ["pending"]


def test_reembed_excludes_soft_deleted_and_tombstoned_heads(tmp_path: Path) -> None:
    """A soft-deleted note and a tombstoned external contribute no live head.

    Not a re-test of live_head_versions' own filtering (covered by
    tests/test_retrieval.py) -- just confirms reembed genuinely delegates to
    it rather than enumerating notes/externals on its own, looser, terms.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
            conn.execute(
                "INSERT INTO versions (version_id, note_id, body, op) "
                "VALUES ('ver-1', 'note-1', 'body', 'delete')"
            )
            conn.execute(
                "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
            )
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES ('ext-1', 'web')"
            )
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES ('snap-1', 'ext-1', 'body text', 'tombstone')"
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = 'snap-1' WHERE external_id = 'ext-1'"
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["reembed", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no live heads to re-embed" in result.stdout


def test_reembed_never_enqueues_enrich_jobs(tmp_path: Path) -> None:
    """reembed forces only the embed leg -- enrich is untouched (lode-g274.7 scope)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
            conn.execute(
                "INSERT INTO versions (version_id, note_id, body, op) "
                "VALUES ('ver-1', 'note-1', 'body', 'create')"
            )
            conn.execute(
                "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["reembed", "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    reader = sqlite3.connect(db_path)
    try:
        types = {r[0] for r in reader.execute("SELECT type FROM jobs").fetchall()}
    finally:
        reader.close()
    assert types == {"embed"}


# --- lode-x9lu: `lode reindex-lexical` -- backfill passages_fts for legacy notes ---


def _fts_rows(db_path: Path, version_id: str) -> list[tuple[str, str]]:
    """Every ``passages_fts`` row keyed to ``version_id``, ordered for comparison."""
    reader = sqlite3.connect(db_path)
    try:
        return reader.execute(
            "SELECT passage_id, text FROM passages_fts WHERE target_version = ? "
            "ORDER BY passage_id",
            (version_id,),
        ).fetchall()
    finally:
        reader.close()


def test_reindex_lexical_backfills_a_note_saved_before_the_lexical_leg(
    tmp_path: Path,
) -> None:
    """A note written straight via versions.save (no cache) has zero FTS rows.

    ``versions.save`` is the lower-level primitive Repository.save wraps with
    the cache seam -- calling it directly, uncached, is exactly what a note
    saved before the lexical leg (lode-x6r.4) landed looks like today: a
    head with no passages/passages_fts rows at all. reindex-lexical must
    backfill it.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-legacy", "hello world, a legacy note")
        version_id = result.version_id
    finally:
        conn.close()

    assert _fts_rows(db_path, version_id) == []

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "reindexed 1 live note head(s)" in result.stdout

    rows = _fts_rows(db_path, version_id)
    assert rows
    assert any("legacy" in text for (_, text) in rows)


def test_reindex_lexical_no_live_notes_is_a_clean_no_op(tmp_path: Path) -> None:
    """An empty corpus reindexes nothing and says so, exiting 0."""
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no live note heads to reindex" in result.stdout


def test_reindex_lexical_is_idempotent(tmp_path: Path) -> None:
    """Re-running against an already-indexed head changes nothing (converges, not duplicates)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-idem", "idempotent body text")
        version_id = result.version_id
    finally:
        conn.close()

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    after_first = _fts_rows(db_path, version_id)
    assert after_first  # the head really did get indexed

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    # The delete-then-insert in LexicalIndex.replace_passages is what makes this
    # converge; passages_fts carries no unique constraint, so without it the second
    # run would simply double the rows. Compare the actual rows, not just a ">0".
    assert _fts_rows(db_path, version_id) == after_first


def test_reindex_lexical_indexes_only_the_head_of_a_version_chain(
    tmp_path: Path,
) -> None:
    """An updated note indexes its head only -- the superseded version gets no rows.

    The command joins ``notes.head_version_id``, so a chain of any length yields
    exactly one row per note and cannot produce duplicate or stale FTS rows for
    the parent version.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        first = save(conn, "note-chain", "the original body text")
        second = save(
            conn, "note-chain", "the revised body text", parent=first.version_id
        )
    finally:
        conn.close()

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "reindexed 1 live note head(s)" in result.stdout

    head_rows = _fts_rows(db_path, second.version_id)
    assert any("revised" in text for (_, text) in head_rows)
    assert _fts_rows(db_path, first.version_id) == []


def test_reindex_lexical_indexes_a_purged_note_head_as_the_marker(
    tmp_path: Path,
) -> None:
    """A purged note's head is still indexed -- as the ``[purged ...]`` marker.

    The executable proof for the "no ``purged_at`` guard" paragraph on
    :func:`lode.cli.reindex_lexical`: :meth:`lode.repository.Repository.purge`
    itself re-indexes the live head from the marker body, so skipping purged
    heads here would diverge from the path this command reproduces.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-purged", "a secret worth purging")
        purge(conn, "note-purged")
    finally:
        conn.close()

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "reindexed 1 live note head(s)" in result.stdout

    rows = _fts_rows(db_path, head.version_id)
    assert rows, "the purged head should still be indexed"
    assert all("secret" not in text for (_, text) in rows)
    assert any("purged" in text for (_, text) in rows)


def test_reindex_lexical_excludes_soft_deleted_notes(tmp_path: Path) -> None:
    """A soft-deleted note's tombstone head is not backfilled."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        head = save(conn, "note-gone", "will be deleted")
        delete(conn, "note-gone", parent=head.version_id)
    finally:
        conn.close()

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no live note heads to reindex" in result.stdout


def test_reindex_lexical_does_not_touch_external_snapshot_rows(tmp_path: Path) -> None:
    """An external snapshot's own FTS rows are untouched -- notes only.

    A live note is present deliberately, so the command actually does work
    (rather than short-circuiting on an empty corpus) and the external's rows
    are proved to survive a real reindex pass, not merely a no-op.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        note = save(conn, "note-alongside", "a live note beside an external")
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES ('ext-1', 'web')"
            )
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES ('snap-1', 'ext-1', 'external snapshot body', 'ok')"
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = 'snap-1' WHERE external_id = 'ext-1'"
            )
            # An external's FTS rows are written by ingest_snapshot in
            # production; simulate one directly here to prove reindex-lexical
            # leaves it alone.
            conn.execute(
                "INSERT INTO passages_fts (passage_id, target_version, text) "
                "VALUES ('p-ext-1', 'snap-1', 'external snapshot body')"
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["reindex-lexical", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "reindexed 1 live note head(s)" in result.stdout

    assert _fts_rows(db_path, "snap-1") == [("p-ext-1", "external snapshot body")]
    assert _fts_rows(db_path, note.version_id)  # the note WAS reindexed


# --- lode-14jr: `lode reenrich` -- targeted, not whole-corpus, regeneration --


def _write_ai_annotation(
    conn: sqlite3.Connection,
    target: str,
    source_version: str,
    model: str,
    provider: str | None = None,
) -> None:
    """``provider`` defaults to ``None`` -- the anthropic convention
    (:func:`lode.llm_provider.provider_identity`, lode-568v.4) -- so every
    existing call site keeps writing an anthropic-produced row unchanged."""
    with conn:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status, model, provider) "
            "VALUES (?, ?, 'summary', ?, 'ai', 'fresh', ?, ?)",
            (target, source_version, json.dumps("a summary"), model, provider),
        )


def test_reenrich_forces_fresh_job_for_a_head_with_a_stale_model(
    tmp_path: Path,
) -> None:
    """A live head whose recorded annotations.model disagrees with the
    currently configured enrichment_llm gets a fresh enrich job -- even past
    a 'done' one, mirroring reembed's force-past-done behavior for embed.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "some-old-model")
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
                "VALUES ('enrich', 'ver-1', 'done', ?)",
                (now_iso(),),
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "enqueued 1 enrich job(s)" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        statuses = [
            r[0]
            for r in reader.execute(
                "SELECT status FROM jobs WHERE type = 'enrich' AND "
                "target_version = 'ver-1' ORDER BY id"
            ).fetchall()
        ]
    finally:
        reader.close()
    # The original 'done' job is untouched; a fresh 'pending' one sits beside it.
    assert statuses == ["done", "pending"]


def test_reenrich_skips_a_head_already_on_the_current_model(tmp_path: Path) -> None:
    """A head whose annotations already agree with enrichment_llm is left alone."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", Settings().enrichment_llm.model)
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no stale enrichment found" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        (count,) = reader.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'enrich'"
        ).fetchone()
    finally:
        reader.close()
    assert count == 0


def test_reenrich_skips_a_head_never_enriched(tmp_path: Path) -> None:
    """A head with no ai annotations at all is unenriched, not stale -- reconcile's
    enrich_gap step owns that case; reenrich must enqueue nothing for it."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no stale enrichment found" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        (count,) = reader.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'enrich'"
        ).fetchone()
    finally:
        reader.close()
    assert count == 0


def test_reenrich_covers_a_stale_external_head_too(tmp_path: Path) -> None:
    """An external's current head_snapshot_id is force-enqueued too, mirroring
    reembed's/live_head_versions' notes-UNION-externals scope -- even though
    enrich_gap itself checks notes only."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES ('ext-1', 'web')"
            )
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
                "VALUES ('snap-1', 'ext-1', 'body text', 'ok')"
            )
            conn.execute(
                "UPDATE externals SET head_snapshot_id = 'snap-1' WHERE external_id = 'ext-1'"
            )
        _write_ai_annotation(conn, "ext-1", "snap-1", "some-old-model")
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "enqueued 1 enrich job(s)" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        statuses = [
            r[0]
            for r in reader.execute(
                "SELECT status FROM jobs WHERE type = 'enrich' AND target_version = 'snap-1'"
            ).fetchall()
        ]
    finally:
        reader.close()
    assert statuses == ["pending"]


def test_reenrich_excludes_no_egress_content_even_if_stale(tmp_path: Path) -> None:
    """A no_egress note/external is never swept in, even with stale annotations --
    enrichment leaves the box, unlike embed, and no_egress exists to prevent that."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id, no_egress) VALUES ('note-1', 1)")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "some-old-model")
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no stale enrichment found" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        (count,) = reader.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'enrich'"
        ).fetchone()
    finally:
        reader.close()
    assert count == 0


def test_reenrich_excludes_soft_deleted_and_tombstoned_heads(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'delete')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "some-old-model")
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no stale enrichment found" in result.stdout


def test_reenrich_no_live_heads_is_a_clean_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "no stale enrichment found" in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        (count,) = reader.execute("SELECT COUNT(*) FROM jobs").fetchone()
    finally:
        reader.close()
    assert count == 0


def test_reenrich_never_enqueues_embed_jobs(tmp_path: Path) -> None:
    """reenrich forces only the enrich leg -- embed is untouched (lode-14jr scope)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "some-old-model")
    finally:
        conn.close()

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    reader = sqlite3.connect(db_path)
    try:
        types = {r[0] for r in reader.execute("SELECT type FROM jobs").fetchall()}
    finally:
        reader.close()
    assert types == {"enrich"}


def test_status_hints_enrichment_stale_when_two_models_disagree_with_config(
    tmp_path: Path,
) -> None:
    """Two distinct stored models, both != current config -- the old
    2+-distinct 'mixed' case, still covered by the new stale-vs-current-config
    check (a strict superset once scoped to live heads)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "model-a")
        _write_ai_annotation(conn, "note-1", "ver-1", "model-b")
    finally:
        conn.close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    # A short substring, not the full sentence: rich wraps the hint line at
    # the terminal width in a test runner, so a phrase spanning the wrap
    # point would match against a newline instead of a space (same pattern
    # as the revision_mixed/revision_drift hint assertions above).
    assert "disagree with the currently" in result.stdout
    assert "configured enrichment_llm" in result.stdout
    assert "No action needed." not in result.stdout


def test_status_hints_enrichment_stale_uniform_disagreement(tmp_path: Path) -> None:
    """lode-o9k3: the gap `_enrichment_model_mixed` missed -- a corpus
    uniformly enriched under a SINGLE model that differs from the currently
    configured enrichment_llm (the primary "just bumped enrichment_llm"
    workflow) must fire the hint, even though there is only one distinct
    stored model on record."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "some-old-model")
    finally:
        conn.close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "disagree with the currently" in result.stdout
    assert "configured enrichment_llm" in result.stdout
    assert "No action needed." not in result.stdout


def test_status_no_enrichment_hint_when_all_one_model_matches_config(
    tmp_path: Path, warm_model_cache: None
) -> None:
    """A single stored model that agrees with the current config stays quiet --
    matches `lode reenrich`'s own "nothing stale" verdict for the same DB.

    Uses `warm_model_cache` because this asserts the all-clear footer ("No
    action needed."): on a machine with no pulled weights -- every CI runner --
    the cold-cache probe would otherwise inject its own "Action needed" line and
    sink the assertion (the exact failure the fixture's docstring describes)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", Settings().enrichment_llm.model)
        # This test bypasses Repository.save (direct row inserts), so it must
        # also seed the passages_fts row save would have written synchronously
        # -- otherwise the lode-cyly lexical-gap hint correctly fires for it,
        # sinking this test's "No action needed." assertion.
        conn.execute(
            "INSERT INTO passages_fts (passage_id, target_version, text) "
            "VALUES ('p-1', 'ver-1', 'body')"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "Action needed" not in result.stdout
    assert "No action needed." in result.stdout


def test_enrichment_model_stale_is_never_fatal(tmp_path: Path) -> None:
    # A directory in place of the db file: sqlite3 can't open it, raising
    # inside _open_db -- the failure path _enrichment_model_stale must
    # swallow rather than propagate (mirrors _model_revision_status's own
    # "Never raises" contract, per the other status-hint probes).
    not_a_db = tmp_path / "not-a-db"
    not_a_db.mkdir()

    assert cli._enrichment_model_stale(not_a_db, "x", None) is False


# --- lode-568v.6: provider joins model in the staleness identity --


def test_stale_enrichment_heads_flags_provider_mismatch_with_matching_model(
    tmp_path: Path,
) -> None:
    """Same model, different provider: the model comparison alone would miss
    this -- a provider switch with the model/deployment string held constant
    is exactly the gap lode-568v.6 closes."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        # Recorded under a non-anthropic provider; current provider is
        # anthropic (None) -- same model string either side.
        _write_ai_annotation(conn, "note-1", "ver-1", "shared-model", "azure_openai")

        heads = cli._stale_enrichment_heads(conn, "shared-model", None)
    finally:
        conn.close()
    assert heads == ["ver-1"]


def test_stale_enrichment_heads_flags_provider_mismatch_other_direction(
    tmp_path: Path,
) -> None:
    """The NULL-safe comparison must catch the switch in the other direction
    too: an anthropic-produced row (provider NULL, by convention) compared
    against a currently-active non-anthropic provider."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        # provider=None -- the anthropic convention (lode-568v.4).
        _write_ai_annotation(conn, "note-1", "ver-1", "shared-model")

        heads = cli._stale_enrichment_heads(conn, "shared-model", "azure_openai")
    finally:
        conn.close()
    assert heads == ["ver-1"]


def test_stale_enrichment_heads_clean_when_model_and_provider_both_match(
    tmp_path: Path,
) -> None:
    """A non-anthropic provider that agrees on both model and provider is not
    stale -- this only reduces to the anthropic/anthropic case by coincidence
    in every other test in this file."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "shared-model", "azure_openai")

        heads = cli._stale_enrichment_heads(conn, "shared-model", "azure_openai")
    finally:
        conn.close()
    assert heads == []


def test_enrichment_model_stale_true_on_provider_switch_alone(tmp_path: Path) -> None:
    """The lode-o9k3 status-hint wrapper reads the same provider-aware query."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(conn, "note-1", "ver-1", "shared-model")
    finally:
        conn.close()

    assert cli._enrichment_model_stale(db_path, "shared-model", "azure_openai") is True


def test_reenrich_forces_fresh_job_on_provider_switch_with_same_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lode reenrich` re-enqueues a head whose model already agrees with
    config but whose recorded provider no longer does -- the CLI-level
    counterpart of the direct query tests above, wired through
    `lode.llm_provider.provider_identity`."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        # Same model as the active config, but recorded under a different
        # provider than the one now active.
        _write_ai_annotation(
            conn, "note-1", "ver-1", Settings().enrichment_llm.model, "azure_openai"
        )
    finally:
        conn.close()

    monkeypatch.setattr(cli, "provider_identity", lambda _settings: None)

    result = runner.invoke(app, ["reenrich", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "enqueued 1 enrich job(s)" in result.stdout


def test_status_hints_enrichment_stale_on_provider_switch_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_model_cache: None
) -> None:
    """`lode status` fires the same hint when only the provider disagrees --
    the acceptance criterion this ticket exists to satisfy."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
        _write_ai_annotation(
            conn, "note-1", "ver-1", Settings().enrichment_llm.model, "azure_openai"
        )
    finally:
        conn.close()

    monkeypatch.setattr(cli, "provider_identity", lambda _settings: None)

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "disagree with the currently" in result.stdout
    assert "configured enrichment_llm" in result.stdout
    assert "No action needed." not in result.stdout


def test_status_hints_lexical_gap(tmp_path: Path, warm_model_cache: None) -> None:
    """A live note head with no passages_fts rows surfaces a lexical-gap hint (lode-cyly)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "1 live note head(s) have no lexical" in result.stdout
    assert "reindex-lexical" in result.stdout
    assert "No action needed." not in result.stdout


def test_status_no_lexical_gap_hint_when_fts_rows_present(
    tmp_path: Path, warm_model_cache: None
) -> None:
    """A note head with a passages_fts row already present stays quiet."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO notes (note_id) VALUES ('note-1')")
        conn.execute(
            "INSERT INTO versions (version_id, note_id, body, op) "
            "VALUES ('ver-1', 'note-1', 'body', 'create')"
        )
        conn.execute(
            "UPDATE notes SET head_version_id = 'ver-1' WHERE note_id = 'note-1'"
        )
        conn.execute(
            "INSERT INTO passages_fts (passage_id, target_version, text) "
            "VALUES ('p-1', 'ver-1', 'body')"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "lexical" not in result.stdout
    assert "No action needed." in result.stdout


def test_lexical_gap_count_is_never_fatal(tmp_path: Path) -> None:
    """A directory in place of the db file: sqlite3 can't open it -- must swallow, not propagate."""
    not_a_db = tmp_path / "not-a-db"
    not_a_db.mkdir()
    assert cli._lexical_gap_count(not_a_db) == 0


def test_status_dead_line_is_uniformly_danger_not_repr_highlighted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_model_cache: None
) -> None:
    """The dead count must render `danger`, not rich's repr.number cyan (lode-re0s).

    rich's Console runs ReprHighlighter over plain strings BY DEFAULT, repainting
    bare values from its own repr.* palette -- which is NOT in CLI_STYLES. On this
    ticket that lands on the one character that matters: with the highlighter on,
    "dead-letters (dead jobs): 3" renders the 3 in bold CYAN while the rest of the
    line is danger red, so the digit distinguishing 3 from 0 is the only digit not
    coloured -- defeating lode-l38d.6's headline requirement ("dead > 0 should
    render red, which is what stops '3' from looking like '0'"). Fixed at the call
    site with highlight=False; hoisting it onto the shared Console is deliberately
    left to lode-re0s, which owns that decision once the sibling branches land.

    The module-level `console` freezes its colour decision at IMPORT (lode-xgaa),
    which is off under the suite -- so this swaps in a force_terminal Console to
    make colour observable at all. Without that, any assertion here is vacuous.
    Proved non-vacuous by sabotage: dropping `highlight=False` from the
    dead-letters print turns this red assertion cyan and the test fails.

    Seeds a ``refresh`` dead-letter, not ``enrich`` -- lode-8vcq softened an
    ALL-self-healing (embed/enrich) dead set to `warn`, so `danger` is only
    still guaranteed for a set containing a terminal (``refresh``) job; that
    softening is covered separately by
    ``test_status_hints_self_healing_dead_letters`` and its sibling below,
    not here. This test's job is only the highlight=False regression.
    """
    import io

    from rich.console import Console

    from lode.cli import CLI_THEME

    # The rebind below targets THIS module's `console` name, not the package's
    # (lode-nftw) -- status.py imports `console` plainly, so its own namespace
    # is the only binding a substitute Console can reach.
    from lode.cli import status as cli_status

    db_path = tmp_path / "lode.db"
    _insert_dead_job(db_path, "refresh", "ver-cccccccccccccccc")

    buf = io.StringIO()
    monkeypatch.setattr(
        cli_status,
        "console",
        Console(theme=CLI_THEME, force_terminal=True, width=100, file=buf),
    )
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0

    dead_line = next(ln for ln in buf.getvalue().splitlines() if "dead-letters" in ln)
    # bold red (danger) present, bold cyan (repr.number) absent.
    assert "\x1b[1;31m" in dead_line
    assert "\x1b[1;36m" not in dead_line


def test_status_dead_line_softens_to_warn_when_only_self_healing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_model_cache: None
) -> None:
    """lode-8vcq, acceptance #5: an ALL-self-healing dead set (embed/enrich,
    no terminal ``refresh``) renders `warn` (yellow), not `danger` (bold
    red) -- the uniform danger red is what made these look ominous despite
    needing no user action. Deliberately updates the uniformity
    ``test_status_dead_line_is_uniformly_danger_not_repr_highlighted`` used
    to pin, per that ticket's own acceptance criterion.
    """
    import io

    from rich.console import Console

    from lode.cli import CLI_THEME
    from lode.cli import status as cli_status

    db_path = tmp_path / "lode.db"
    _insert_dead_job(db_path, "enrich", "ver-cccccccccccccccc")

    buf = io.StringIO()
    monkeypatch.setattr(
        cli_status,
        "console",
        Console(theme=CLI_THEME, force_terminal=True, width=100, file=buf),
    )
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0

    dead_line = next(ln for ln in buf.getvalue().splitlines() if "dead-letters" in ln)
    # plain yellow (warn), not bold red (danger).
    assert "\x1b[33m" in dead_line
    assert "\x1b[1;31m" not in dead_line


def _write_fake_cache_hit(home: Path, hf_source: str, model_file: str) -> None:
    """Build the minimal on-disk layout `try_to_load_from_cache` recognizes.

    Mirrors real HuggingFace cache structure closely enough to satisfy
    `try_to_load_from_cache`'s own resolution (verified against the installed
    `huggingface_hub`'s source): with no `refs/` dir present it looks for a
    snapshot folder literally named `"main"` (its default revision), so
    `snapshots/main/<model_file>` as a real file is sufficient -- no `blobs/`
    symlink or `refs/main` commit-hash file needed for a cache HIT.
    """
    snapshot = (
        home
        / "models"
        / f"models--{hf_source.replace('/', '--')}"
        / "snapshots"
        / "main"
    )
    file_path = snapshot / model_file
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("{}")


def test_model_cache_probe_warm_and_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The probe is keyed by the entry's `sources.hf` repo id, NOT by the
    # friendly model id in settings -- the two differ for some models, so a
    # probe keyed on the model id would report a warm cache cold forever.
    from lode.cli import _model_cache_probe
    from lode.config import model_cache_identity

    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    model_id = Settings().embedding_model
    hf_source, model_file = model_cache_identity(model_id)  # type: ignore[misc]

    # Nothing on disk yet -> confirmed cold.
    assert _model_cache_probe(model_id) is False

    # A models--X dir with NO completed snapshot is still cold: HuggingFace's
    # downloader creates `blobs/` with an `.incomplete` file BEFORE a download
    # finishes, so "the directory exists" cannot mean "warm" -- this is the
    # coupled partial-download bug lode-l38d.6's review found and this pin's
    # switch to `try_to_load_from_cache` fixes.
    repo_dir = home / "models" / f"models--{hf_source.replace('/', '--')}"
    (repo_dir / "blobs").mkdir(parents=True)
    (repo_dir / "blobs" / "deadbeef.incomplete").write_text("partial")
    assert _model_cache_probe(model_id) is False

    # Completed snapshot -> warm.
    _write_fake_cache_hit(home, hf_source, model_file)
    assert _model_cache_probe(model_id) is True


def test_model_cache_probe_matches_model_id_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fastembed resolves model ids case-insensitively, so the probe must too --
    # otherwise a config.toml with a case-variant id loads fine everywhere else
    # in lode while the probe reports "cannot judge" and the cold hint can never
    # fire for it.
    from lode.cli import _model_cache_probe
    from lode.config import model_cache_identity

    home = tmp_path / "home"
    monkeypatch.setenv("LODE_HOME", str(home))
    model_id = Settings().embedding_model
    hf_source, model_file = model_cache_identity(model_id)  # type: ignore[misc]
    _write_fake_cache_hit(home, hf_source, model_file)

    assert _model_cache_probe(model_id.upper()) is True
    assert _model_cache_probe(model_id.lower()) is True


def test_model_cache_probe_unknown_model_cannot_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An id in no fastembed registry is un-judgeable, which is None ("could not
    # judge"), NOT False ("confirmed cold") -- the distinction is what stops a
    # user who pinned a custom model from being nagged to `lode models pull`
    # forever by a probe that can never turn warm.
    from lode.cli import _cold_model_cache, _model_cache_probe

    monkeypatch.setenv("LODE_HOME", str(tmp_path / "home"))
    # One call now covers what took two: the probe searches BOTH registries
    # (they are disjoint), so a None here means neither list matched.
    assert _model_cache_probe("not-a-real/model-id") is None

    # ...and None must not read as cold at the caller: an all-unknown settings
    # set produces no hint, per the probe's non-fatal contract.
    settings = Settings(
        embedding_model="not-a-real/model-id",
        rerank_model="not-a-real/model-id",
        entailment_model="not-a-real/model-id",
    )
    assert _cold_model_cache(settings) is False


def test_cold_model_cache_is_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    # lode-l38d.6 requires the probe to be non-fatal: `lode status` was a pure
    # DB read before it, and a footer hint must never be able to take the
    # command down. Force the probe's internals to raise and assert the caller
    # still just answers "not cold".
    from lode import cli

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("cache dir exploded")

    monkeypatch.setattr(cli, "model_cache_dir", _boom)
    assert cli._cold_model_cache(Settings()) is False


def test_status_survives_a_malformed_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The test above asserts non-fatality of everything BELOW settings
    # resolution -- it hands _cold_model_cache a ready-made Settings(), so it is
    # structurally blind to a failure resolving them. That blind spot is exactly
    # where the footer's own settings lookup went fatal: _resolve_settings()
    # echoes + raises typer.Exit(1) on a bad config.toml (lode-40g), so an
    # UNGUARDED call made `lode status` exit 1 over a config typo, after the
    # table had printed -- no footer at all, which is decision 3's failure mode
    # (an absent hint read as an absent check) and breaches lode-l38d.6's
    # explicit "never a failed `lode status`". Assert at the COMMAND boundary,
    # the only altitude that sees it.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("embedding_model = [not valid toml\n")
    monkeypatch.setenv("LODE_HOME", str(home))

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = CliRunner().invoke(app, ["status", "--db", str(db_path)])

    # Queue health needs no config -- a broken one must not take the command
    # down (trunk exited 0 here; so must we).
    assert result.exit_code == 0, result.output
    # ...and the footer must still be REACHED. A cold-cache hint is suppressed
    # (settings unresolvable -> "no hint", same as the probe's own None), so
    # with an empty queue this is the all-clear.
    assert "No action needed." in result.output


def test_status_survives_an_unreadable_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The malformed case above is the one _resolve_settings converts to
    # typer.Exit itself. This is the OTHER half, and the reason the guard above
    # catches Exception rather than typer.Exit: load_settings() propagates an
    # OSError (e.g. PermissionError on an unreadable $LODE_HOME/config.toml)
    # straight THROUGH _resolve_settings, which only converts TOMLDecodeError
    # and ValidationError. A narrow `except typer.Exit` would leave this case
    # killing `lode status` exactly as before. Driven by monkeypatch rather than
    # chmod 000, which is a no-op when the suite runs as root.
    from lode import cli

    def _boom() -> Settings:
        raise PermissionError("config.toml is not readable")

    monkeypatch.setattr(cli, "_resolve_settings", _boom)

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = CliRunner().invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "No action needed." in result.output


def test_status_all_clear_when_no_pending_failed_and_cache_warm(
    tmp_path: Path, warm_model_cache: None
) -> None:
    # Warm cache (pinned by the fixture) + no pending/failed jobs -> the
    # explicit all-clear line, not silence.
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version, status, attempts, next_attempt_at) "
                "VALUES ('embed', 'ver-dddddddddddddddd', 'done', 1, ?)",
                (now_iso(),),
            )
    finally:
        conn.close()
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "No action needed." in result.stdout
    assert "Action needed" not in result.stdout


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


def test_egress_lists_a_tool_row_whose_model_is_null(tmp_path: Path) -> None:
    """A purpose='tool' row has no model (lode-35nu.11.7) and must still list.

    The schema admits a NULL model for a tool call, so the listing's column
    padding has to survive one -- no writer produces such a row yet
    (lode-35nu.11.1 does), which is exactly why it needs a test now rather than
    a TypeError later.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO egress_log (purpose, destination, arguments, "
                "sent_targets) VALUES ('tool', 'https://acme.atlassian.net', "
                "'{\"jql\": \"x\"}', '[]')"
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["egress", "--db", str(db_path)])
    assert result.exit_code == 0, result.stdout
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "tool" in lines[0]


def test_egress_purpose_tool_filter_and_destination_arguments(tmp_path: Path) -> None:
    """--purpose tool narrows to exactly the tool rows and renders dest/args.

    Seeds an enrich/qa pair (_seed_egress) plus a purpose='tool' row so the
    filter has non-tool rows to exclude, then asserts the surviving row's
    destination/arguments render (lode-l87l acceptance #1/#2).
    """
    db_path = tmp_path / "lode.db"
    _seed_egress(db_path)
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO egress_log (purpose, destination, arguments, "
                "sent_targets) VALUES ('tool', 'https://acme.atlassian.net', "
                "'{\"jql\": \"x\"}', '[]')"
            )
    finally:
        conn.close()

    result = runner.invoke(app, ["egress", "--purpose", "tool", "--db", str(db_path)])
    assert result.exit_code == 0, result.stdout
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "enrich" not in result.stdout
    assert "qa" not in result.stdout
    assert "destination: https://acme.atlassian.net" in lines[0]
    assert 'arguments: {"jql": "x"}' in lines[0]


def test_egress_purpose_tool_round_trips_a_row_written_by_fetch_for_ask(
    tmp_path: Path,
) -> None:
    """A real ``tools.fetch_for_ask`` write is filterable via ``--purpose tool``.

    End-to-end check that the writer (lode.tools, lode-35nu.11.1) and the
    reader (this ticket, lode-l87l) agree on the row shape -- not just a
    hand-inserted row.
    """
    from lode.drawdown import SOURCE_TYPE_WEB
    from lode.tools import fetch_for_ask
    from lode.webfetch import RawResponse

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    url = "https://example.com/article"
    try:

        class _StubFetcher:
            def fetch(self, target_url: str) -> RawResponse:
                return RawResponse(
                    final_url=target_url,
                    status_code=200,
                    text="<html><body><article><p>"
                    + ("Real article content. " * 20)
                    + "</p></article></body></html>",
                )

        fetch_for_ask(
            conn,
            url,
            SOURCE_TYPE_WEB,
            fetcher=_StubFetcher(),
            settings=load_settings(),
        )
    finally:
        conn.close()

    result = runner.invoke(app, ["egress", "--purpose", "tool", "--db", str(db_path)])
    assert result.exit_code == 0, result.stdout
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "tool" in lines[0]
    assert f"destination: {url}" in lines[0]


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


def test_purge_hard_deletes_a_note_and_reports_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "secret hunter2", "--db", str(db_path)]
    ).stdout.strip()

    result = runner.invoke(app, ["purge", note_id, "--db", str(db_path)])
    assert result.exit_code == 0
    assert note_id in result.stdout  # it reports what it swept, not refuses

    # The body is overwritten with the [purged YYYY-MM-DD] marker and purged_at set.
    marker = f"[purged {datetime.now(UTC):%Y-%m-%d}]"
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

    ``_short_date`` now converts the stored UTC ``created`` to system local
    time before formatting (lode-olmi.5); this test pins ``TZ=UTC`` so that
    conversion is a no-op and the raw-UTC-slice assertion below stays valid
    regardless of the host machine's real timezone.
    """
    import lode.enrich as enrich_mod

    def _fake_enrich(conn, version_id, settings, *, client=None):
        pass

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich)

    with _local_tz("UTC"):
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
    # Regression pin (lode-bau6/lode-l38d.12): the live listing must render
    # byte-identical to before the --deleted marker was added -- no marker
    # leaks onto a live row.
    assert "[deleted]" not in result.stdout


def test_notes_date_column_renders_in_local_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lode notes``' date column converts stored UTC to local time (lode-olmi.5).

    Pins a fixed, non-UTC, no-DST offset (``Etc/GMT+5`` == UTC-5) so the
    displayed date/time is deterministically shifted from the raw UTC
    timestamp, and asserts the *converted* wall clock appears -- not the
    raw UTC slice.
    """
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"

    with _local_tz("Etc/GMT+5"):
        note_id = runner.invoke(
            app, ["add", "a note for the local-time test", "--db", str(db_path)]
        ).stdout.strip()

        result = runner.invoke(app, ["notes", "--db", str(db_path)])

    assert result.exit_code == 0
    created = _rows(db_path, "SELECT created FROM notes WHERE note_id = ?", (note_id,))[
        0
    ][0]
    utc_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    expected_local = (utc_dt - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
    assert expected_local in result.stdout
    # The whole-hour UTC-5 offset always shifts the displayed hour, so the raw
    # UTC slice can never coincide with the converted wall clock -- it must not
    # appear verbatim in the output.
    raw_utc_slice = created[:16].replace("T", " ")
    assert raw_utc_slice not in result.stdout


def test_notes_excludes_a_tombstoned_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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


def _capture_console_print(
    monkeypatch: pytest.MonkeyPatch,
    console: object | None = None,
) -> list[tuple[str, dict[str, object]]]:
    """Capture every ROW passed to a shared Console's ``print``, with kwargs.

    Defaults to the stdout ``cli.console``; pass ``cli.err_console`` to
    capture the stderr twin instead (lode-l810).

    Rows only: the bare ``console.print()`` that separates notes carries no
    argument and is skipped, so a caller can add a second note without the
    capture blowing up on a missing ``args[0]``.
    """
    printed: list[tuple[str, dict[str, object]]] = []

    def _capture(*args: object, **kwargs: object) -> None:
        if args:
            printed.append((str(args[0]), kwargs))

    monkeypatch.setattr(cli.console if console is None else console, "print", _capture)
    return printed


def test_notes_separates_rows_with_a_blank_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-l38d.5: a blank line separates each note from the next -- but
    there is no trailing blank line after the last row."""
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
    runner.invoke(app, ["add", "first note", "--db", str(db_path)])
    runner.invoke(app, ["add", "second note", "--db", str(db_path)])

    result = runner.invoke(app, ["notes", "--db", str(db_path)])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines.count("") == 1  # exactly one separator, between the 2 rows
    assert lines[-1] != ""  # no trailing blank line after the last note


def test_notes_colours_id_and_date_through_the_shared_theme_and_escapes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-l38d.5: the id/date render via the shared theme's ``note_id``/
    ``date`` style NAMES (lode-l38d.11) -- never a hand-rolled colour literal
    -- and the summary is markup-escaped so a literal ``[`` in note text can
    never be mistaken for rich markup.

    NOTE ON WHAT THIS CAN AND CANNOT ASSERT (lode-xgaa -- do not "simplify"
    this back): the shared ``console`` froze its colour decision at IMPORT
    time, so no ANSI is emitted under the suite and no assertion here can
    prove colour is actually APPLIED. That is the residual risk the
    lode-l38d.1 /challenge decision accepted knowingly (positive path verified
    BY EYE, no test seam). It is emphatically NOT because "CliRunner's output
    is never a TTY" -- that mechanism is FALSE; colour is off only because
    pytest's default capture replaced stdout before ``lode.cli`` was imported,
    and ``pytest -s`` from a real terminal freezes it the other way. See
    tests/test_cli_console.py, which refutes that claim at length, and
    ``cli/__init__.py``'s ``console`` docstring.

    So this test asserts the two things it genuinely can: that ``notes_``
    hands rich the ``[note_id]``/``[date]`` style names (captured in-process),
    and that the summary's markup escaping SURVIVES RENDERING to real stdout.
    """
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
    note_id = runner.invoke(
        app, ["add", "a note with a [bracket] in it", "--db", str(db_path)]
    ).stdout.strip()

    # Assert on the RENDERED output first: the escaped "[bracket]" must reach
    # the user as the literal text they typed. Capturing the pre-render string
    # alone would only prove escape() was called, not that rich renders it
    # back correctly -- the round-trip is the behaviour that matters.
    rendered = runner.invoke(app, ["notes", "--db", str(db_path)])
    assert rendered.exit_code == 0
    assert "a note with a [bracket] in it" in rendered.stdout
    assert "\\[bracket]" not in rendered.stdout  # the escape must not leak

    printed = _capture_console_print(monkeypatch)

    result = runner.invoke(app, ["notes", "--db", str(db_path)])

    assert result.exit_code == 0
    assert len(printed) == 1
    line, kwargs = printed[0]
    assert f"[note_id]{note_id}[/note_id]" in line
    assert "[date]" in line and "[/date]" in line
    # The literal "[bracket]" in the note text must be ESCAPED (rich.markup's
    # backslash convention), not left as unescaped markup that could corrupt
    # the row or the styles around it.
    assert "\\[bracket]" in line
    assert "[bracket]" not in line.replace("\\[bracket]", "")
    # Pin the per-call rendering flag. Asserted here rather than left to
    # eye-verification precisely because the suite can never catch a
    # regression by eye: colour is frozen off at import (see this test's
    # docstring). ``highlight`` is NOT asserted here (lode-re0s) -- it is no
    # longer a per-call kwarg at all, having been hoisted onto the shared
    # ``console`` itself; see tests/test_cli_console.py's
    # test_console_highlight_is_disabled for that pin instead.
    assert kwargs["soft_wrap"] is True


# --- lode notes --deleted (list tombstoned notes, lode-d32.2) ---------------


def test_notes_deleted_flag_lists_only_tombstoned_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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


def test_notes_deleted_flag_marks_each_row_with_the_deleted_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-bau6/lode-l38d.12: the human decision was to DISTINGUISH tombstoned
    rows with a trailing ``" [deleted]"`` marker (the same convention ``show``
    and ``_report_ambiguous_prefix``/lode-l38d.10 already use), not a colour
    cue.

    Asserted against REAL rendered stdout (not just the pre-render markup
    string) because the marker text itself -- ``" [deleted]"`` -- contains
    ``[...]``, which rich's ``Console.print`` parses as a markup tag. Verified
    against rich 15.0.0 (the same finding lode-l810 made at a sibling call
    site): an unknown style name like "deleted" does not raise, it resolves to
    a null style and is silently eaten, so an unescaped marker would render as
    nothing at all -- the tombstone cue vanishing is exactly the regression
    this test exists to catch. This is NOT dependent on colour: color being
    frozen off under the suite (lode-xgaa) only means no ANSI is emitted, not
    that markup tags stop being parsed -- an unescaped ``[deleted]`` vanishes
    from plain rendered stdout with or without a real terminal.
    """
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
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
    assert "gone soon [deleted]" in result.stdout


def test_notes_deleted_flag_marker_survives_a_summary_containing_brackets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary that itself contains ``"[deleted]"`` must still be escaped,
    and the row must still end with exactly one trailing marker -- the
    escape(summary) call already guarded against confusion with markup
    (lode-l38d.5); this pins that the marker addition doesn't regress it.
    """
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
    gone_id = runner.invoke(
        app, ["add", "note text with [deleted] already in it", "--db", str(db_path)]
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
    # The summary's own literal "[deleted]" plus the trailing marker -- both
    # survive rendering, verbatim, as plain text.
    assert "note text with [deleted] already in it [deleted]" in result.stdout


def test_notes_deleted_flag_also_colours_id_and_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--deleted`` renders through the SAME styled path as the live listing
    (lode-l38d.5) -- no separate hand-rolled formatting for tombstoned rows --
    but now also appends the trailing ``" [deleted]"`` marker (lode-bau6's
    human decision, built here per lode-l38d.12).
    """
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
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

    printed = _capture_console_print(monkeypatch)

    result = runner.invoke(app, ["notes", "--deleted", "--db", str(db_path)])

    assert result.exit_code == 0
    assert len(printed) == 1
    line, kwargs = printed[0]
    assert f"[note_id]{gone_id}[/note_id]" in line
    assert "[date]" in line and "[/date]" in line
    # The marker is escaped TOGETHER with the summary (a bare "\[deleted]" in
    # the pre-render markup string, rich's escape() convention for a literal
    # "[") -- not appended after escaping, which would leave an unescaped,
    # swallowed markup tag. See this module's rendered-output test
    # (test_notes_deleted_flag_marks_each_row_with_the_deleted_marker) for the
    # behavioural proof; this pins the mechanism.
    assert "gone soon \\[deleted]" in line
    # ``highlight`` is no longer a per-call kwarg (lode-re0s hoisted it onto
    # the shared ``console`` itself) -- see test_cli_console.py's
    # test_console_highlight_is_disabled for that pin instead.
    assert kwargs["soft_wrap"] is True  # same rendering flag as the live path


def test_notes_deleted_flag_says_no_deleted_notes_when_none_are_tombstoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty message names the queried scope -- a live note exists here."""
    _noop_enrich(monkeypatch)
    db_path = tmp_path / "lode.db"
    runner.invoke(app, ["add", "still here", "--db", str(db_path)])

    result = runner.invoke(app, ["notes", "--deleted", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "no deleted notes"


# --- lode purge <prefix> (unambiguous note-id prefix, lode-1gr.3) -----------


def test_purge_accepts_an_unambiguous_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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
    assert "ambiguous note id prefix 'note-aaa': 2 matches" in result.stderr
    # Self-sufficient (lode-l38d.10): a full row -- id, date, summary -- per
    # candidate, not just its bare id, so no second command is needed to tell
    # them apart.
    assert re.search(
        r"note-aaa111 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +body a$",
        result.stderr,
        re.MULTILINE,
    )
    assert re.search(
        r"note-aaa222 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +body b$",
        result.stderr,
        re.MULTILINE,
    )

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


def test_purge_empty_prefix_purges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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
            "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
            "VALUES ('enrich', ?, 'pending', ?)",
            (pending.version_id, now_iso()),
        )

        failed = save(conn, "note-failed", "enrich dead-lettered")
        conn.execute(
            "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
            "VALUES ('enrich', ?, 'dead', ?)",
            (failed.version_id, now_iso()),
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


def test_show_accepts_an_unambiguous_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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
    assert "ambiguous note id prefix 'note-bbb': 2 matches" in result.stderr
    assert re.search(
        r"note-bbb111 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +body a$",
        result.stderr,
        re.MULTILINE,
    )
    assert re.search(
        r"note-bbb222 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +body b$",
        result.stderr,
        re.MULTILINE,
    )


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


def test_show_live_note_has_no_deleted_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _noop_enrich(monkeypatch)
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


# --- lode dump-html <note> [<selector>] (spec 06 item 7c, lode-olmi.7) -----


def _seed_external_edge(
    conn: sqlite3.Connection,
    note_id: str,
    version_id: str,
    *,
    external_id: str,
    snapshot_id: str,
    raw_payload: str | None,
    status: str = "ok",
) -> None:
    """Draw down one external and wire an edge to it from ``note_id``.

    Mirrors ``_seed_external`` (the ``show`` external-snapshot fixture) but
    also writes ``raw_payload`` -- ``dump-html``'s whole reason for being --
    which that fixture never needed to set.
    """
    with conn:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, 'web')",
            (external_id,),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, raw_payload, status, fetched_at) "
            "VALUES (?, ?, 'body', ?, ?, '2026-07-08T00:00:00.000000Z')",
            (snapshot_id, external_id, raw_payload, status),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, ?, 'user', 'pasted URL', 1.0, ?, 'fresh')",
            (note_id, external_id, version_id),
        )


def test_dump_html_single_external_prints_raw_payload(tmp_path: Path) -> None:
    """The common case: one drawn-down external, no selector needed."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-dump-1", "see https://example.com/article")
        _seed_external_edge(
            conn,
            "note-dump-1",
            result.version_id,
            external_id="https://example.com/article",
            snapshot_id="snap-dump-1",
            raw_payload="<html><body>hello</body></html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-1", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "<html><body>hello</body></html>"


def test_dump_html_accepts_an_unambiguous_note_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-dump-prefix", "see https://example.com/x")
        _seed_external_edge(
            conn,
            "note-dump-prefix",
            result.version_id,
            external_id="https://example.com/x",
            snapshot_id="snap-dump-prefix",
            raw_payload="<html>x</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-pref", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "<html>x</html>"


def test_dump_html_no_external_sources_reports_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-dump-none", "just a plain note")
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-none", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "no external sources" in result.stderr


def test_dump_html_multi_external_no_selector_lists_them(tmp_path: Path) -> None:
    """Ambiguous (>1 external, no selector): list rather than guess."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(
            conn, "note-dump-multi", "see https://a.example and https://b.example"
        )
        _seed_external_edge(
            conn,
            "note-dump-multi",
            result.version_id,
            external_id="https://a.example",
            snapshot_id="snap-dump-a",
            raw_payload="<html>a</html>",
        )
        _seed_external_edge(
            conn,
            "note-dump-multi",
            result.version_id,
            external_id="https://b.example",
            snapshot_id="snap-dump-b",
            raw_payload="<html>b</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-multi", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "https://a.example" in result.stdout
    assert "https://b.example" in result.stdout
    assert "<html>a</html>" not in result.stdout
    assert "<html>b</html>" not in result.stdout


def test_dump_html_multi_external_selector_by_index(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(
            conn, "note-dump-idx", "see https://a.example and https://b.example"
        )
        _seed_external_edge(
            conn,
            "note-dump-idx",
            result.version_id,
            external_id="https://a.example",
            snapshot_id="snap-dump-idx-a",
            raw_payload="<html>a</html>",
        )
        _seed_external_edge(
            conn,
            "note-dump-idx",
            result.version_id,
            external_id="https://b.example",
            snapshot_id="snap-dump-idx-b",
            raw_payload="<html>b</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app, ["dump-html", "note-dump-idx", "2", "--db", str(db_path)]
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "<html>b</html>"


def test_dump_html_multi_external_selector_by_id(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(
            conn, "note-dump-sel", "see https://a.example and https://b.example"
        )
        _seed_external_edge(
            conn,
            "note-dump-sel",
            result.version_id,
            external_id="https://a.example",
            snapshot_id="snap-dump-sel-a",
            raw_payload="<html>a</html>",
        )
        _seed_external_edge(
            conn,
            "note-dump-sel",
            result.version_id,
            external_id="https://b.example",
            snapshot_id="snap-dump-sel-b",
            raw_payload="<html>b</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        ["dump-html", "note-dump-sel", "https://a.example", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "<html>a</html>"


def test_dump_html_unmatched_selector_reports_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(
            conn, "note-dump-bad-sel", "see https://a.example and https://b.example"
        )
        _seed_external_edge(
            conn,
            "note-dump-bad-sel",
            result.version_id,
            external_id="https://a.example",
            snapshot_id="snap-dump-bad-a",
            raw_payload="<html>a</html>",
        )
        _seed_external_edge(
            conn,
            "note-dump-bad-sel",
            result.version_id,
            external_id="https://b.example",
            snapshot_id="snap-dump-bad-b",
            raw_payload="<html>b</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        ["dump-html", "note-dump-bad-sel", "no-such-thing", "--db", str(db_path)],
    )

    assert result.exit_code != 0
    assert "no external source matching" in result.stderr


def test_dump_html_tombstone_reports_cleanly_instead_of_dumping_empty(
    tmp_path: Path,
) -> None:
    """A tombstoned (link-rotted) snapshot has no raw HTML to dump -- report,
    don't print an empty line (this ticket's acceptance criteria)."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-dump-dead", "see https://dead.example")
        _seed_external_edge(
            conn,
            "note-dump-dead",
            result.version_id,
            external_id="https://dead.example",
            snapshot_id="snap-dump-dead",
            raw_payload=None,
            status="tombstone",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-dead", "--db", str(db_path)])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "no stored HTML" in result.stderr


def test_dump_html_ok_snapshot_with_no_raw_payload_reports_cleanly(
    tmp_path: Path,
) -> None:
    """An ``ok`` snapshot can still have a NULL raw_payload (e.g. a redirect-cap
    tombstone predecessor never applies here, but the column is nullable
    regardless of status) -- same clean report, not an empty dump."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-dump-noraw", "see https://noraw.example")
        _seed_external_edge(
            conn,
            "note-dump-noraw",
            result.version_id,
            external_id="https://noraw.example",
            snapshot_id="snap-dump-noraw",
            raw_payload=None,
            status="ok",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-noraw", "--db", str(db_path)])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "no stored HTML" in result.stderr


def test_dump_html_unknown_note_reports_and_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["dump-html", "no-such-note", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "no such note" in result.stderr


def test_dump_html_ambiguous_note_prefix_reports_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-dump-ambig-1", "a")
        save(conn, "note-dump-ambig-2", "b")
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "note-dump-ambig", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "ambiguous note id prefix 'note-dump-ambig': 2 matches" in result.stderr
    # A full candidate row -- id, date, summary -- not just a bare id
    # (lode-l38d.10): each candidate's body ("a"/"b") IS its summary here
    # (no annotation, so it falls back to the first line).
    assert re.search(
        r"note-dump-ambig-1 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +a$",
        result.stderr,
        re.MULTILINE,
    )
    assert re.search(
        r"note-dump-ambig-2 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +b$",
        result.stderr,
        re.MULTILINE,
    )


# --- lode dump-html --all / --file (bulk dumping, lode-l38d.8) -------------


def test_dump_html_all_no_file_prints_delimited_concatenation(tmp_path: Path) -> None:
    """--all without --file: stdout, ==> id url <== headers, blank line between."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        r1 = save(conn, "note-all-1", "see https://one.example")
        _seed_external_edge(
            conn,
            "note-all-1",
            r1.version_id,
            external_id="https://one.example",
            snapshot_id="snap-all-1",
            raw_payload="<html>one</html>",
        )
        r2 = save(conn, "note-all-2", "see https://two.example")
        _seed_external_edge(
            conn,
            "note-all-2",
            r2.version_id,
            external_id="https://two.example",
            snapshot_id="snap-all-2",
            raw_payload="<html>two</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "--all", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "==> note-all-2  https://two.example <==" in result.stdout
    assert "==> note-all-1  https://one.example <==" in result.stdout
    assert "<html>one</html>" in result.stdout
    assert "<html>two</html>" in result.stdout


def test_dump_html_all_skips_notes_with_no_external(tmp_path: Path) -> None:
    """Under --all, a note with nothing to dump is silently skipped -- no error."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-all-bare", "just a plain note, no externals")
        r = save(conn, "note-all-has", "see https://has.example")
        _seed_external_edge(
            conn,
            "note-all-has",
            r.version_id,
            external_id="https://has.example",
            snapshot_id="snap-all-has",
            raw_payload="<html>has</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "--all", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "<html>has</html>" in result.stdout
    assert "note-all-bare" not in result.stdout


def test_dump_html_all_skips_tombstoned_and_no_raw_payload_externals(
    tmp_path: Path,
) -> None:
    """Under --all, an external with nothing captured is skipped, not an error."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        r = save(conn, "note-all-dead", "see https://dead.example")
        _seed_external_edge(
            conn,
            "note-all-dead",
            r.version_id,
            external_id="https://dead.example",
            snapshot_id="snap-all-dead",
            raw_payload=None,
            status="tombstone",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["dump-html", "--all", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "no external HTML captured for any note" in result.stdout


def test_dump_html_all_file_writes_one_file_per_external_zero_padded(
    tmp_path: Path,
) -> None:
    """--all --file DIR: one 0-padded-suffix file per dumpable external."""
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        result = save(
            conn, "note-all-multi", "see https://a.example and https://b.example"
        )
        _seed_external_edge(
            conn,
            "note-all-multi",
            result.version_id,
            external_id="https://a.example",
            snapshot_id="snap-all-multi-a",
            raw_payload="<html>a</html>",
        )
        _seed_external_edge(
            conn,
            "note-all-multi",
            result.version_id,
            external_id="https://b.example",
            snapshot_id="snap-all-multi-b",
            raw_payload="<html>b</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        ["dump-html", "--all", "--file", "--dir", str(out_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert (out_dir / "note-all-multi-0001.dmp").read_text() == "<html>a</html>"
    assert (out_dir / "note-all-multi-0002.dmp").read_text() == "<html>b</html>"
    assert "wrote 2 file(s)" in result.stdout


def test_dump_html_all_file_suffixes_single_external_unconditionally(
    tmp_path: Path,
) -> None:
    """The 0-padded suffix is unconditional, even for a note's only external."""
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-all-single", "see https://only.example")
        _seed_external_edge(
            conn,
            "note-all-single",
            result.version_id,
            external_id="https://only.example",
            snapshot_id="snap-all-single",
            raw_payload="<html>only</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        ["dump-html", "--all", "--file", "--dir", str(out_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert (out_dir / "note-all-single-0001.dmp").read_text() == "<html>only</html>"


def test_dump_html_all_file_creates_directory_if_absent(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "does" / "not" / "exist" / "yet"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-all-mkdir", "see https://mkdir.example")
        _seed_external_edge(
            conn,
            "note-all-mkdir",
            result.version_id,
            external_id="https://mkdir.example",
            snapshot_id="snap-all-mkdir",
            raw_payload="<html>mkdir</html>",
        )
        conn.commit()
    finally:
        conn.close()

    assert not out_dir.exists()

    result = runner.invoke(
        app,
        ["dump-html", "--all", "--file", "--dir", str(out_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert (out_dir / "note-all-mkdir-0001.dmp").exists()


def test_dump_html_all_file_bare_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare --file (no --dir) writes into the current working directory."""
    db_path = tmp_path / "lode.db"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    conn = init_db(db_path)
    try:
        result = save(conn, "note-all-cwd", "see https://cwd.example")
        _seed_external_edge(
            conn,
            "note-all-cwd",
            result.version_id,
            external_id="https://cwd.example",
            snapshot_id="snap-all-cwd",
            raw_payload="<html>cwd</html>",
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.chdir(cwd)
    result = runner.invoke(app, ["dump-html", "--all", "--file", "--db", str(db_path)])

    assert result.exit_code == 0
    assert (cwd / "note-all-cwd-0001.dmp").read_text() == "<html>cwd</html>"


def test_dump_html_all_file_overwrites_existing_file(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "note-all-over-0001.dmp").write_text("stale")
    conn = init_db(db_path)
    try:
        result = save(conn, "note-all-over", "see https://over.example")
        _seed_external_edge(
            conn,
            "note-all-over",
            result.version_id,
            external_id="https://over.example",
            snapshot_id="snap-all-over",
            raw_payload="<html>fresh</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        ["dump-html", "--all", "--file", "--dir", str(out_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert (out_dir / "note-all-over-0001.dmp").read_text() == "<html>fresh</html>"


def test_dump_html_all_with_explicit_target_is_an_arity_error(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(
        app, ["dump-html", "--all", "some-note", "--db", str(db_path)]
    )

    assert result.exit_code != 0
    assert "--all" in result.stderr


def test_dump_html_single_target_file_writes_without_all(tmp_path: Path) -> None:
    """--file no longer requires --all: a single target writes its own file.

    Post-technical-review user correction (lode-l38d.8): the original
    "--file requires --all" arity check is wrong -- --file with an explicit
    target should write that one dump to a file, not error.
    """
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-file-noall", "see https://noall.example")
        _seed_external_edge(
            conn,
            "note-file-noall",
            result.version_id,
            external_id="https://noall.example",
            snapshot_id="snap-file-noall",
            raw_payload="<html>x</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        [
            "dump-html",
            "note-file-noall",
            "--file",
            "--dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    written = out_dir / "note-file-noall-0001.dmp"
    assert written.read_text() == "<html>x</html>"
    assert str(written) in result.stdout


def test_dump_html_single_target_file_bare_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare --file (no --dir, no --all) writes into the current directory."""
    db_path = tmp_path / "lode.db"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    conn = init_db(db_path)
    try:
        result = save(conn, "note-file-cwd", "see https://filecwd.example")
        _seed_external_edge(
            conn,
            "note-file-cwd",
            result.version_id,
            external_id="https://filecwd.example",
            snapshot_id="snap-file-cwd",
            raw_payload="<html>cwd</html>",
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.chdir(cwd)
    result = runner.invoke(
        app, ["dump-html", "note-file-cwd", "--file", "--db", str(db_path)]
    )

    assert result.exit_code == 0
    assert (cwd / "note-file-cwd-0001.dmp").read_text() == "<html>cwd</html>"


def test_dump_html_single_target_file_multi_external_uses_selector_index(
    tmp_path: Path,
) -> None:
    """The written file's NNNN matches the note's dumpable-external listing index."""
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        result = save(
            conn, "note-file-multi", "see https://a.example and https://b.example"
        )
        _seed_external_edge(
            conn,
            "note-file-multi",
            result.version_id,
            external_id="https://a.example",
            snapshot_id="snap-file-multi-a",
            raw_payload="<html>a</html>",
        )
        _seed_external_edge(
            conn,
            "note-file-multi",
            result.version_id,
            external_id="https://b.example",
            snapshot_id="snap-file-multi-b",
            raw_payload="<html>b</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        [
            "dump-html",
            "note-file-multi",
            "2",
            "--file",
            "--dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    assert (out_dir / "note-file-multi-0002.dmp").read_text() == "<html>b</html>"
    assert not (out_dir / "note-file-multi-0001.dmp").exists()


def test_dump_html_single_target_file_duplicate_externals_use_listed_index(
    tmp_path: Path,
) -> None:
    """A duplicate edge's NNNN is its own listing position, not the first equal one.

    ``edges`` has no ``(from_id, to_id)`` unique constraint and ``lode.enrich``
    inserts an ``ai`` edge without dedup against an existing one, so a note can
    list the same external twice. :class:`ExternalView` is a frozen dataclass
    (equal by value), so recovering the choice's position with
    ``externals.index(chosen)`` would find the FIRST equal entry: selecting the
    listing's entry 2 would write ``-0001.dmp``, colliding with entry 1's file
    and disagreeing with the ``--all`` path, which numbers by position.
    """
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-file-dup", "see https://dup.example")
        _seed_external_edge(
            conn,
            "note-file-dup",
            result.version_id,
            external_id="https://dup.example",
            snapshot_id="snap-file-dup",
            raw_payload="<html>dup</html>",
        )
        # A second, AI-inferred edge to the SAME external -- what enrich writes.
        conn.execute(
            "INSERT INTO edges (from_id, to_id, source, reason, confidence, "
            "source_version, status) "
            "VALUES (?, ?, 'ai', 'inferred', 0.9, ?, 'fresh')",
            ("note-file-dup", "https://dup.example", result.version_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        [
            "dump-html",
            "note-file-dup",
            "2",
            "--file",
            "--dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    assert (out_dir / "note-file-dup-0002.dmp").read_text() == "<html>dup</html>"
    assert not (out_dir / "note-file-dup-0001.dmp").exists()


def test_dump_html_single_target_file_still_errors_on_nothing_to_dump(
    tmp_path: Path,
) -> None:
    """--file doesn't bypass the single-target path's existing "nothing to
    dump" errors -- a note with no external sources still fails cleanly."""
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        save(conn, "note-file-none", "just a plain note")
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        [
            "dump-html",
            "note-file-none",
            "--file",
            "--dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code != 0
    assert "no external sources" in result.stderr
    assert not out_dir.exists()


def test_dump_html_no_target_and_no_all_is_an_arity_error(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["dump-html", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "target is required unless --all is given" in result.stderr


def test_dump_html_file_with_no_target_and_no_all_reuses_the_target_check(
    tmp_path: Path,
) -> None:
    """--file with neither a target nor --all errors via the EXISTING target check.

    lode-l38d.8's post-review user decision: this combination stays an error,
    but deliberately NOT through a check of its own -- "the existing 'no target
    and no --all' rejection already covers it. Do not add a second one." So the
    message must be that check's, not a --file-specific one.
    """
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["dump-html", "--file", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "target is required unless --all is given" in result.stderr


def test_dump_html_dir_without_file_is_an_arity_error(tmp_path: Path) -> None:
    """--dir without --file must fail, not be silently ignored while stdout wins.

    --dir only means anything in file-output mode; accepting it bare would
    silently discard the user's stated intent (write files into DIR) and dump
    HTML to stdout instead -- the same "given vs absent is indistinguishable"
    trap that sank the ticket's original single tri-state --file, which is why
    --dir defaults to None rather than Path(".").
    """
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-dir-nofile", "see https://nofile.example")
        _seed_external_edge(
            conn,
            "note-dir-nofile",
            result.version_id,
            external_id="https://nofile.example",
            snapshot_id="snap-dir-nofile",
            raw_payload="<html>x</html>",
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app, ["dump-html", "--all", "--dir", str(out_dir), "--db", str(db_path)]
    )

    assert result.exit_code != 0
    assert "--dir requires --file" in result.stderr
    assert not out_dir.exists()


def test_dump_html_all_file_writes_non_ascii_payload_as_utf8(tmp_path: Path) -> None:
    """Fetched HTML is routinely non-ASCII: pin the on-disk bytes to UTF-8.

    ``Path.write_text`` with no explicit encoding uses the locale's preferred
    encoding, so under a C/POSIX locale (cron, systemd, minimal containers)
    the unfixed write raised UnicodeEncodeError mid-sweep, after partially
    writing the batch -- hence the explicit ``encoding="utf-8"``.

    This asserts the CONTRACT (UTF-8 on disk), not that failure mode: it does
    not reproduce it, and passes either way under a UTF-8 locale. Forcing it
    in-process is not possible -- CPython resolves the default encoding in C
    at open() time, so monkeypatching ``locale.getpreferredencoding`` does not
    reach it -- and a ``LC_ALL=C`` subprocess would stop reproducing it under
    PEP 686's UTF-8-by-default anyway. It still guards the real contract: a
    non-UTF-8 explicit encoding here would fail it.
    """
    db_path = tmp_path / "lode.db"
    out_dir = tmp_path / "out"
    payload = "<html>café — ünïcode … 日本語</html>"
    conn = init_db(db_path)
    try:
        result = save(conn, "note-all-utf8", "see https://utf8.example")
        _seed_external_edge(
            conn,
            "note-all-utf8",
            result.version_id,
            external_id="https://utf8.example",
            snapshot_id="snap-all-utf8",
            raw_payload=payload,
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        ["dump-html", "--all", "--file", "--dir", str(out_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0
    written = out_dir / "note-all-utf8-0001.dmp"
    assert written.read_bytes() == payload.encode("utf-8")
    assert written.read_text(encoding="utf-8") == payload


# --- lode recover <prefix> (undo a soft-delete, lode-d32.3) ----------------


def test_recover_round_trip_reappears_in_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete -> recover: the note comes back into 'lode notes' and its FTS row."""
    _noop_enrich(monkeypatch)
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


def test_recover_live_note_errors_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovering a note that isn't tombstoned errors -- nothing to recover."""
    _noop_enrich(monkeypatch)
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
    assert "ambiguous note id prefix 'note-ddd': 2 matches" in result.stderr
    # The live candidate renders unmarked; the tombstoned one gets the
    # ` [deleted]` marker (lode-l38d.10's WRINKLE) -- for `recover` that's the
    # candidate the user actually wants, so it must not render blank or look
    # identical to the live match.
    assert re.search(
        r"note-ddd111 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +still live$",
        result.stderr,
        re.MULTILINE,
    )
    assert re.search(
        r"note-ddd222 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +gone soon \[deleted\]$",
        result.stderr,
        re.MULTILINE,
    )


def test_ambiguous_prefix_rows_render_like_notes_through_err_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-l810: candidate rows render on notes_'s exact rendering path.

    The whole point of this ticket is that the two listings' shared columns
    cannot diverge again, so the things that made them diverge are pinned
    here rather than left to eye-verification: the suite freezes colour off
    at import (lode-xgaa), so a regression in the style names or the
    soft_wrap flag would sail through green -- which is exactly how the
    ReprHighlighter date-shredding defect reached trunk in lode-l38d.5.
    (``highlight=False`` is no longer a per-call flag to pin here -- it is
    hoisted onto ``err_console``'s constructor, lode-9jmv, and pinned by
    ``test_cli_console.py``'s ``test_err_console_highlight_is_disabled``.)

    Rationale for soft_wrap lives at the ``cli/__init__.py`` call site, deliberately not
    restated here (see notes_'s equivalent pin).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        save(conn, "note-fff111", "a summary with a [bracket] in it")
        r2 = save(conn, "note-fff222", "gone")
        delete(conn, "note-fff222", parent=r2.version_id)
    finally:
        conn.close()

    printed = _capture_console_print(monkeypatch, cli.err_console)

    result = runner.invoke(app, ["recover", "note-fff", "--db", str(db_path)])

    assert result.exit_code == 1
    assert len(printed) == 2
    for line, kwargs in printed:
        # Semantic style NAMES, never a colour literal -- CLI_STYLES stays
        # the one source of truth (lode-l38d.11).
        assert "[note_id]" in line and "[/note_id]" in line
        assert "[date]" in line and "[/date]" in line
        assert kwargs["soft_wrap"] is True

    live_row, deleted_row = printed[0][0], printed[1][0]
    # Markup in the user's summary must be escaped, not left to corrupt the
    # row or the styles around it.
    assert "\\[bracket]" in live_row
    assert "[bracket]" not in live_row.replace("\\[bracket]", "")
    # The tombstone marker must reach rich ESCAPED -- unescaped, rich parses
    # "[deleted]" as a style tag and consumes it, and the marker vanishes.
    assert "\\[deleted]" in deleted_row


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
    assert "ambiguous note id prefix 'note-eee': 2 matches" in result.stderr
    assert re.search(
        r"note-eee111 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +gone a \[deleted\]$",
        result.stderr,
        re.MULTILINE,
    )
    assert re.search(
        r"note-eee222 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +gone b \[deleted\]$",
        result.stderr,
        re.MULTILINE,
    )


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
    monkeypatch.setattr(
        "lode.qa.build_provider", lambda settings: AnthropicProvider(client)
    )
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


def test_ask_exits_nonzero_with_actionable_message_on_llm_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode ask' fails loud and clean on a non-auth LLMProviderError (lode-yx1c).

    AuthError and LLMProviderError are SIBLING RuntimeError subclasses -- neither
    is an ancestor of the other -- so a handler that only named AuthError let a
    raw traceback through here too. ``cited_answer.ask`` is stubbed to raise
    directly; an empty corpus (retrieval finds nothing to rerank) keeps the
    offline embedder stub sufficient without pulling in the real cross-encoder.

    The ``result.exception`` assertion is the load-bearing one, here and in the
    three tests below it, for the reason spelled out at
    ``test_work_rejects_an_invalid_config_file``: ``CliRunner`` reports
    ``exit_code == 1`` for an *unhandled* exception too, so only the exception's
    type distinguishes a clean error from a crash. (An ``assert "Traceback" not
    in result.stdout`` cannot do that job -- ``CliRunner`` captures an escaping
    exception rather than rendering it, so no traceback text reaches either
    stream either way.)
    """
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()  # empty corpus: nothing to retrieve
    _offline_embedder(monkeypatch)

    def _raise(*args, **kwargs):
        raise LLMProviderError("provider returned 500", provider="anthropic")

    monkeypatch.setattr("lode.cited_answer.ask", _raise)

    result = runner.invoke(app, ["ask", "anything at all?", "--db", str(db_path)])

    assert result.exit_code == 1
    assert "provider returned 500" in result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_ask_exits_nonzero_with_actionable_message_on_llm_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode ask' fails loud and clean on LLMAuthError too (lode-568v.3, lode-yx1c).

    LLMAuthError subclasses LLMProviderError, not AuthError, so it hit the same
    gap as the plain LLMProviderError case above.
    """
    db_path = tmp_path / "lode.db"
    init_db(db_path).close()  # empty corpus: nothing to retrieve
    _offline_embedder(monkeypatch)

    def _raise(*args, **kwargs):
        raise LLMAuthError("no OpenAI/Azure credentials (test)", provider="openai")

    monkeypatch.setattr("lode.cited_answer.ask", _raise)

    result = runner.invoke(app, ["ask", "anything at all?", "--db", str(db_path)])

    assert result.exit_code == 1
    assert "no OpenAI/Azure credentials (test)" in result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


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
        context = retrieval._retrieve(
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
    """retrieval._retrieve calls rerank() between RRF and expand_parents (lode-vtf).

    Two notes are indexed through the dense leg with orthogonal vectors so RRF
    ranks the 'first' passage ahead of the 'second' one by construction (see
    ``_TwoDirEmbedder``). A stubbed cross-encoder — injected via
    ``lode.retrieval.FastEmbedCrossEncoder``, the seam ``rerank()`` falls back to
    when no scorer is passed — scores the 'second' passage higher. Asserting the
    live ``_retrieve`` path (not just ``rerank()``'s own unit tests) returns
    'second' ahead of 'first' proves rerank actually fires from
    ``lode.retrieval._retrieve``'s composed pipeline.
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
        context = retrieval._retrieve(
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

        context = retrieval._retrieve(
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
    """retrieval._retrieve calls graph_expand() after expand_parents (lode-vtf).

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

        context = retrieval._retrieve(
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
    rendered honestly rather than assumed away — mirrors ``lode.tui.services.ask``."""
    line = cli._format_citation(Support(version_id="missing", quoted_span="x"), None)

    assert "as of unknown" in line


def test_format_cited_answer_surfaces_withheld_even_on_abstention() -> None:
    answer = CitedAnswer(claims=(), withheld_citations=(WithheldCitation("v-secret"),))

    lines = cli._format_cited_answer(answer, {})

    assert lines[0] == cli._ABSTAIN_LINE
    assert any("v-secret" in line and "withheld" in line for line in lines)


# --- lode config (resolved paths read-out, lode-ftc) ------------------------


def test_config_surfaces_every_resolved_path_under_lode_home(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # Acceptance: $LODE_HOME root, DB, vector store, model cache dir, log dir,
    # and config file path are all displayed, resolved under the single root
    # (docs/configuration.md §Paths & locations) -- the full set of paths that
    # table documents (lode-agh: model cache was missing here).
    set_console_width(1000)
    home = tmp_path / "home"
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    out = result.stdout
    assert str(home) in out  # the resolved root
    assert str(home / "lode.db") in out
    assert str(home / "lode.db.lock") in out
    assert str(home / "lancedb") in out
    assert str(home / "models") in out
    assert str(home / "logs") in out
    assert str(home / "config.toml") in out


def test_config_reports_config_file_present_or_absent(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # The optional config.toml is shown absent by default, present once it exists.
    set_console_width(1000)
    home = tmp_path / "home"
    home.mkdir()
    absent = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert absent.exit_code == 0
    assert "config.toml" in absent.stdout
    assert "(absent)" in absent.stdout

    (home / "config.toml").write_text("", encoding="utf-8")
    present = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert "config.toml" in present.stdout
    assert "(present)" in present.stdout


def test_config_flags_env_override_vs_default(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # The effective source of the root is surfaced: env override when set, else
    # the ~/.lode default (docs design: "show the effective env-var override").
    set_console_width(1000)
    home = tmp_path / "home"
    overridden = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert "($LODE_HOME)" in overridden.stdout

    default = runner.invoke(app, ["config"], env={"LODE_HOME": ""})
    assert "(default)" in default.stdout


def test_config_db_override_shifts_displayed_db_and_vector_store(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # A per-invocation --db override moves the displayed DB, its lock, and the
    # co-located vector store; the root/logs/config still come from $LODE_HOME.
    set_console_width(1000)
    home = tmp_path / "home"
    custom_db = tmp_path / "elsewhere" / "custom.db"
    result = runner.invoke(
        app,
        ["config", "--db", str(custom_db)],
        env={"LODE_HOME": str(home)},
    )
    assert result.exit_code == 0
    out = result.stdout
    assert str(custom_db) in out
    assert str(tmp_path / "elsewhere" / "custom.db.lock") in out
    assert str(tmp_path / "elsewhere" / "lancedb") in out
    # logs and config stay under the root, not beside the overridden DB.
    assert str(home / "logs") in out
    assert str(home / "config.toml") in out


# --- lode config knob table (lode-juz8.6) ------------------------------------


def test_config_shows_every_runtime_and_tune_knob_with_current_value(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # Acceptance: every runtime+tune Settings knob appears with its CURRENT
    # resolved value and kind, even with no config.toml present (shows
    # defaults) -- the knob table sits below the existing paths block.
    set_console_width(1000)
    home = tmp_path / "home"
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    for name, value, kind in config.knob_rows(config.Settings()):
        assert name in result.stdout
        assert value in result.stdout
        assert kind in result.stdout


def test_config_excludes_build_kind_knobs(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # SCOPE decision (lode-juz8.6): build-kind knobs (imply a rebuild/
    # migration, e.g. embedding_model/embedding_vector_dim/content_hash) are
    # hidden from the knob table.
    set_console_width(1000)
    home = tmp_path / "home"
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    knob_names = {name for name, _, _ in config.knob_rows(config.Settings())}
    assert "embedding_model" not in knob_names
    assert "content_hash" not in knob_names
    for _, _, kind in config.knob_rows(config.Settings()):
        assert kind != config.Kind.BUILD.value
    assert "embedding_model" not in result.stdout
    assert "nomic-ai/nomic-embed-text-v1.5" not in result.stdout


def test_config_knob_table_reflects_config_toml_override(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # A config.toml override for a runtime knob shows up as the CURRENT
    # value, not the field default -- confirms the table reads a resolved
    # Settings instance (load_settings), not bare field defaults.
    set_console_width(1000)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("retrieval_top_k = 42\n", encoding="utf-8")
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    assert "retrieval_top_k" in result.stdout
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("retrieval_top_k")
    ]
    assert len(lines) == 1
    assert "42" in lines[0]


# --- lode config terminal-width awareness (lode-l38d.4) ----------------------


def test_config_wraps_long_knob_values_without_losing_characters(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # THE BUG THIS TICKET FIXES: at a normal 80-column terminal, a long
    # list-valued knob (the ~255-char redaction pattern lists) used to inflate
    # every row's padding to its own width; now it wraps within its own
    # column instead. Assert no data is lost in the wrap.
    set_console_width(80)
    home = tmp_path / "home"
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    longest_name, longest_value, _ = max(
        config.knob_rows(config.Settings()), key=lambda row: len(row[1])
    )
    assert len(longest_value) > 80  # the row genuinely needs to wrap at 80 cols
    assert longest_name in result.stdout
    # No ellipsis, and no character loss. NOTE (lode-1apg): this does NOT
    # guard overflow="fold" specifically, despite appearances. Knob values
    # are ", "-joined (see knob_rows), so rich's default word-wrap already
    # breaks this cell at the ", " boundaries -- overflow="ellipsis" only
    # ever truncates a piece that is STILL too wide after wrapping (i.e. an
    # unbreakable single token), which a ", "-joined list never is. Proven by
    # sabotage: removing overflow="fold" from the knob table's Value column
    # leaves this test passing. The genuinely fold-dependent case is a long
    # UNBREAKABLE value (e.g. a path with no spaces) -- see
    # test_config_path_table_folds_long_unbreakable_path_without_truncating
    # below, which IS proven non-vacuous by that same sabotage.
    assert "…" not in result.stdout
    # The value's own distinctive tail token survives intact -- short enough
    # to fit the Value column at COLUMNS=80 without itself needing a further
    # fold-break, so it is a reliable "nothing before this was truncated"
    # witness (an ellipsis-truncated render would never reach it at all).
    last_token = longest_value.rsplit(", ", 1)[-1]
    assert last_token in result.stdout


def test_config_path_table_folds_long_unbreakable_path_without_truncating(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # lode-1apg finding 2: overflow="fold" on the PATH table (_config_path_
    # table) is genuinely load-bearing but was untested -- every existing
    # path-table test forces width=1000 with a short tmp_path home, so none
    # can ever hit a column overflow. Unlike the knob table's ", "-joined
    # values (see the previous test), a filesystem path has no space to wrap
    # at -- it is a single unbreakable token. At a normal 80-column
    # terminal, rich's DEFAULT overflow="ellipsis" truncates an unbreakable
    # token wider than its column and drops characters; overflow="fold"
    # hard-breaks it instead, losing nothing.
    set_console_width(80)
    # A single long, space-free path component -- guaranteed wider than any
    # plausible Value column at 80 total columns, and unbreakable by rich's
    # word-based wrapping (no spaces to wrap at).
    home = tmp_path / ("x" * 200)
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    out = result.stdout
    assert "…" not in out
    # rich's "fold" hard-breaks mid-word by inserting a line break, not by
    # dropping characters -- so the full path survives as a CONTIGUOUS
    # substring once whitespace (the very line breaks fold introduces, plus
    # ordinary column padding) is stripped back out. An ellipsis-truncating
    # renderer could never satisfy this: it drops characters rather than
    # merely relocating them across lines.
    squashed = "".join(out.split())
    assert str(home / "lode.db") in squashed


def test_config_output_has_no_ansi_when_piped(tmp_path: Path) -> None:
    # Acceptance: output degrades cleanly when piped -- no ANSI escapes.
    #
    # NAME/SCOPE CORRECTED (lode-1apg): this used to be named
    # ...no_ansi_under_no_color and claimed to exercise NO_COLOR, but it does
    # not. subprocess.run's captured stdout is a PIPE, so rich's console
    # already sees is_terminal=False and suppresses ANSI for THAT reason,
    # regardless of NO_COLOR -- verified by the control: with NO_COLOR
    # removed from env entirely, no_color resolves False but is_terminal is
    # still False, and this same assertion still holds. So this test
    # genuinely covers "no ANSI when piped" (part of .4's acceptance), not
    # NO_COLOR detection. NO_COLOR is still set below (harmless, matches how
    # a real piped invocation is typically run) but is not what makes the
    # assertion pass.
    #
    # The NO_COLOR *mechanism* itself is already covered non-vacuously,
    # including an env-absent control, by tests/test_cli_console.py
    # (lode-xgaa) -- that is the canonical pattern for any future test that
    # actually needs to exercise NO_COLOR detection: a fresh subprocess with
    # the env set before import, not a monkeypatch after.
    home = tmp_path / "home"
    env = {**os.environ, "LODE_HOME": str(home), "NO_COLOR": "1", "COLUMNS": "80"}
    result = subprocess.run(
        [sys.executable, "-m", "lode.cli", "config"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "\x1b[" not in result.stdout


# --- CLI rich Table markup-injection guard, shared seam (lode-9tmd) --------
#
# THE INVARIANT: rich parses a bare `str` cell as MARKUP, so a literal
# "[...]" substring (a redaction regex character class, an absolute path,
# anything) is silently DROPPED. lode-l38d.4 fixed this per call site by
# wrapping every cell in `Text(...)`; lode-9tmd moves the guard into
# `cli.SafeTable.add_row` itself so a future table cannot reintroduce the bug
# by forgetting to wrap. Three tests below: (1) the guard itself, proven
# non-vacuous by a same-input control against plain `rich.table.Table`; (2) a
# structural test that fails for ANY table in the cli package built via bare `Table(`
# instead of `SafeTable(`; (3) the full CLI-level round trip named in the
# ticket's acceptance criteria.


def test_safe_table_round_trips_a_bracketed_cell_where_plain_table_drops_it() -> None:
    # Non-vacuity proof, inline rather than merely asserted: the SAME input
    # rendered through plain rich.table.Table on the same Console settings
    # DOES lose the bracketed content -- this is the exact defect lode-9tmd
    # closes, not a hypothetical. If SafeTable's add_row override were ever
    # deleted (or a cell type check were mistakenly narrowed), this test
    # would start failing on the SafeTable assertion below.
    cell = "gh[pousr]_[0-9a-zA-Z]{36}"

    control = Table()
    control.add_column("Value")
    control.add_row(cell)
    control_buf = io.StringIO()
    Console(file=control_buf, width=80, no_color=True).print(control)
    assert cell not in control_buf.getvalue()  # the bug, reproduced

    guarded = cli.SafeTable()
    guarded.add_column("Value")
    guarded.add_row(cell)
    guarded_buf = io.StringIO()
    Console(file=guarded_buf, width=80, no_color=True).print(guarded)
    assert cell in guarded_buf.getvalue()  # the fix: byte-for-byte round trip


def test_every_cli_table_construction_routes_through_safe_table() -> None:
    # Structural guard (the ticket's own acceptance criterion: "a test fails
    # for ANY table that passes a bare str, so a future third table cannot
    # silently reintroduce the character drop"). Enforced here by making it
    # IMPOSSIBLE to construct a plain rich.table.Table anywhere in the cli package
    # outside SafeTable's own class body -- a bare-str cell is only ever
    # dangerous on an unguarded Table, so barring the construction bars the
    # whole defect class regardless of how a future call site writes its
    # add_row calls.
    # lode-35nu.9: lode.cli is now a PACKAGE (src/lode/cli/**), not one file --
    # scan EVERY module in it, `__init__.py`/`__main__.py` included, not just
    # `cli.__file__` (which is now only this package's own __init__.py). No
    # module is exempted: SafeTable's own class body lives in __init__.py and
    # still contains no `\bTable\(` for the scan to trip on (see the regex
    # comment below), exactly as when this was one flat file.
    cli_dir = Path(cli.__file__).parent
    assert cli_dir.name == "cli"  # sanity: still lode.cli, not some other package
    py_files = sorted(cli_dir.glob("*.py"))
    # A glob that silently matches nothing would make this whole guard pass
    # vacuously -- the one failure mode the single-file version could not have.
    # Pin both that the scan found files AND that it reached the package's own
    # __init__.py, the module the pre-split premise was entirely about.
    assert len(py_files) > 1, f"cli package scan matched nothing in {cli_dir}"
    assert cli_dir / "__init__.py" in py_files
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        # \bTable\( (not \bSafeTable\() -- word-boundary regex so a legitimate
        # `SafeTable(...)` construction (which itself contains the substring
        # "Table(") never false-positives: there is no \b between "Safe" and
        # "Table" inside one identifier, so this matches only a standalone
        # `Table(` construction. SafeTable's own class body (lode.cli.__init__)
        # needs no exemption: its base-class reference is `SafeTable(Table):`
        # (a `Table)`, not a `Table(`) and its override calls `super().add_row`,
        # so it contains no `\bTable\(` for this scan to trip on.
        bare_construction = re.search(r"\bTable\(", source)
        assert bare_construction is None, (
            f"found a direct rich.table.Table(...) construction in "
            f"lode.cli.{py_file.stem} -- every CLI table must construct a "
            "SafeTable instead (lode-9tmd), or a bare-str cell can silently "
            "drop bracketed content again"
        )


def test_config_knob_table_round_trips_the_github_pat_pattern_at_cli_level(
    tmp_path: Path, set_console_width: Callable[[int], None]
) -> None:
    # Acceptance criterion, verbatim: "A value containing a literal [...]
    # (e.g. gh[pousr]_[0-9a-zA-Z]{36}) round-trips to stdout byte-for-byte."
    # This is the real knob value (config._SECRET_SEED_PATTERNS via
    # Settings.redact_before_egress_patterns default), exercised through the
    # actual `lode config` command end-to-end -- not a synthetic table.
    set_console_width(1000)
    home = tmp_path / "home"
    result = runner.invoke(app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0
    assert "gh[pousr]_[0-9A-Za-z]{36}" in result.stdout


# --- lode work (async worker drain, lode-i05.3) ----------------------------


def _noop_embed_registry() -> dict:
    """A stub registry with a no-op embed handler for offline CLI tests."""
    return {"embed": lambda conn, tv, db, s: None}


# Seed any job this cluster expects `lode work` to CLAIM via
# lode.jobs.enqueue_derive_jobs, never a hand-written INSERT (lode-4e48). The
# hazard is two clocks either side of one comparison: worker._claim_one's
# `next_attempt_at <= now` predicate reads the ratcheted `lode.jobs.now`, so a
# row stamped from SQLite's raw wall clock instead can read as not-yet-due and
# strand every job behind it ("drained 2 job(s)", not 3). enqueue_derive_jobs's
# own docstring works that through in full (lode-0dnk). Seeding through the
# production primitive is eliminative, not merely narrowing: runner.invoke runs
# the CLI in-process, and that clock never decreases within a process.
#
# Since lode-uk1i the column has no SQL DEFAULT at all, so omitting it is an
# outright IntegrityError rather than a silent wrong-clock stamp -- the trap is
# gone, not merely documented. A hand-written INSERT is therefore still fine for
# a row the claim predicate never sees (seeded non-pending, or of a type the
# patched registry excludes -- the ('refresh', 'ver-stuck') rows further down),
# but it must now supply next_attempt_at explicitly; any valid value will do.


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
            # Three embed jobs (no version row needed for the noop handler).
            for i in range(3):
                enqueue_derive_jobs(conn, f"ver-{i}", types=("embed",))
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


def test_work_exits_nonzero_with_actionable_message_on_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' fails loud and clean on a permanent AuthError (lode-9yy).

    A handler that raises AuthError (standing in for a real build_client()
    call with no credentials resolvable) must not be retried or dead-lettered
    -- 'lode work' exits non-zero with build_client's actionable message on
    stderr, no raw traceback.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            enqueue_derive_jobs(conn, "ver-1", types=("embed",))
    finally:
        conn.close()

    def _no_credentials(conn, tv, db, s):
        raise AuthError("no credentials (test)")

    monkeypatch.setattr(worker_mod, "_REGISTRY", {"embed": _no_credentials})

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no credentials (test)" in result.stderr
    # No raw traceback leaked to the user.
    assert "Traceback" not in result.stdout

    reader = sqlite3.connect(db_path)
    try:
        status, attempts = reader.execute(
            "SELECT status, attempts FROM jobs WHERE type = 'embed'"
        ).fetchone()
    finally:
        reader.close()
    assert status == "pending"
    assert attempts == 0  # uncharged — never retried, never dead-lettered


def test_work_exits_nonzero_with_actionable_message_on_llm_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' fails loud and clean on a non-auth LLMProviderError too (lode-yx1c).

    AuthError and LLMProviderError are SIBLING RuntimeError subclasses -- neither
    is an ancestor of the other -- so the handler above (which only named
    AuthError) let a raw traceback through for any LLMProviderError ``drain()``
    re-raises that is not a credential failure (a rate limit, a 500, ...).
    ``drain()`` itself is stubbed to raise directly, isolating the CLI handler;
    the real stash-and-re-raise path it re-raises *from* is exercised by
    ``test_work_renders_a_stuck_batch_poll_cleanly_through_the_real_drain``.
    """

    def _raise(*args, **kwargs):
        raise LLMProviderError("provider returned 500", provider="anthropic")

    monkeypatch.setattr("lode.worker.drain", _raise)

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "provider returned 500" in result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_work_exits_nonzero_with_actionable_message_on_llm_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' fails loud and clean on LLMAuthError too (lode-568v.3, lode-yx1c).

    LLMAuthError subclasses LLMProviderError, not AuthError, so it hit the same
    gap as the plain LLMProviderError case above -- the ``except AuthError``
    handler could not catch it either.
    """

    def _raise(*args, **kwargs):
        raise LLMAuthError("no OpenAI/Azure credentials (test)", provider="openai")

    monkeypatch.setattr("lode.worker.drain", _raise)

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no OpenAI/Azure credentials (test)" in result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_work_renders_a_stuck_batch_poll_cleanly_through_the_real_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stuck-batch path reaches the user as one clean line, end to end.

    The two ``LLMProviderError`` ``work`` tests above stub ``drain`` itself,
    isolating the CLI handler, so neither exercises the compose
    ``docs/storage.md`` actually promises: ``drain`` STASHES a non-auth
    ``LLMProviderError`` raised inside a batch pre-step, finishes the
    credential-free work, and re-raises it only at the end of the pass
    (lode-5zqa) -- and only then does the CLI handler render it (lode-yx1c).
    Both halves run for real here; the single stub is ``collect_enrich_batch``,
    standing in for a permanently malformed batch-results line.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            enqueue_derive_jobs(conn, "ver-1", types=("enrich",))
            # Park it exactly as a submitted batch leaves it: 'running' with a
            # handle, which is what _batch_collect_enrich selects on.
            conn.execute(
                "UPDATE jobs SET status = 'running', batch_handle = 'batch-stuck' "
                "WHERE type = 'enrich'"
            )
    finally:
        conn.close()

    def _poison(*args, **kwargs):
        raise LLMProviderError(
            "batch results line 3 is not valid JSON", provider="anthropic"
        )

    monkeypatch.setattr("lode.enrich.collect_enrich_batch", _poison)

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "batch results line 3 is not valid JSON" in result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


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
            enqueue_derive_jobs(conn, "ver-1", types=("embed",))
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


def test_work_prints_jira_401_outcome_naming_source_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-gpzn.5: a forced 401 on a JIRA refresh job produces a visible,
    actionable line in 'lode work' output naming the source (the JIRA issue
    key) and the reason (the classified tombstone reason) -- the ticket's own
    acceptance criterion, exercised end-to-end through the CLI (not just at
    the drawdown/worker unit level). Token-safety itself is covered where the
    credential is actually in scope: tests/test_jira_fetch.py.
    """
    import lode.worker as worker_mod
    from lode.drawdown import SOURCE_TYPE_JIRA, refresh_external
    from lode.webfetch import RawResponse

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO externals (external_id, source_type, api_base) "
                "VALUES (?, ?, ?)",
                ("ABC-401", SOURCE_TYPE_JIRA, "https://acme.atlassian.net"),
            )
            enqueue_derive_jobs(conn, "ABC-401", types=("refresh",))
    finally:
        conn.close()

    class _UnauthorizedFetcher:
        def fetch(self, url: str) -> RawResponse:
            return RawResponse(final_url=url, status_code=401, text="Unauthorized")

    def _refresh_handler(conn, target_version, db, settings):
        return refresh_external(
            conn, target_version, settings, fetcher=_UnauthorizedFetcher()
        )

    monkeypatch.setattr(worker_mod, "_REGISTRY", {"refresh": _refresh_handler})

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "ABC-401" in result.stdout, "outcome must name the source"
    assert "http_401" in result.stdout, "outcome must name the reason"


def test_work_never_dead_letters_enrich(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' must never dead-letter an enrich job (lode-npx.2 batch path).

    After lode-npx.2 the batch pre-step handles pending enrich jobs via the
    Batches API.  When the version being enriched is not found in the DB (a
    synthetic test case), submit_enrich_batch marks the job 'done' immediately
    (same skip logic as enrich_version).  The job must never become 'dead'.
    """
    import lode.llm_provider as llm_provider_mod
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            enqueue_derive_jobs(conn, "ver-1")  # embed + enrich (no version row)
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())
    # submit_enrich_batch calls build_provider() -> build_client() before its
    # per-row skip gate runs (lode-85q) -- stub it so the missing-version skip
    # path is what's actually exercised here, not a real, un-mocked client
    # construction.
    monkeypatch.setattr(llm_provider_mod, "build_client", lambda: object())

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
            enqueue_derive_jobs(conn, "ver-0", types=("embed",))
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path), "--wait"])
    assert result.exit_code == 0, result.output
    assert "drained 1 job(s)" in result.stdout


def test_work_holds_ONE_embedder_across_every_poll_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode work' builds one FastEmbedEmbedder for the process, not one per pass.

    The PROCESS-level half of lode-j5r2, and the half nothing else pins.
    tests/test_worker.py's own drain-level test proves one embedder serves every
    job *within* one drain; only `work` constructing it outside the polling
    `while True:` makes it one per *process*, which is precisely what four doc
    sites now promise ("one metadata call per process"). Without this test the
    obvious tidy-up -- moving that construction inside the loop for locality --
    reverts the promise with every gate green.

    Asserted two ways, because construction-counting alone would still pass if
    `work` built one embedder and then forgot to pass it: every `drain()` call
    must also receive the *same object*.

    The registry is deliberately NOT patched (unlike its neighbours here): the
    hoist is guarded on `registry["embed"] is worker._embed_handler`, so a stub
    registry would disable the very thing under test. No jobs are queued -- the
    embedder's lifetime is the subject, and the drain-level test already covers
    what happens to jobs.
    """
    from conftest import _OfflineQueryEmbedder

    import lode.embedding as embedding_mod
    import lode.worker as worker_mod

    class _CountingEmbedder(_OfflineQueryEmbedder):
        constructions = 0

        def __init__(self, settings: object) -> None:
            super().__init__(settings)
            _CountingEmbedder.constructions += 1

    monkeypatch.setattr(embedding_mod, "FastEmbedEmbedder", _CountingEmbedder)

    db_path = tmp_path / "lode.db"
    init_db(db_path).close()

    real_drain = worker_mod.drain
    seen: list[object] = []

    def _counting_drain(*args: object, **kwargs: object) -> int:
        seen.append(kwargs.get("embedder"))
        # Two passes are the minimum that can tell "once per process" apart
        # from "once per pass"; stop the endless --loop on the second.
        if len(seen) == 2:
            raise KeyboardInterrupt
        return real_drain(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_mod, "drain", _counting_drain)

    result = runner.invoke(
        app, ["work", "--db", str(db_path), "--loop", "--interval", "0.1"]
    )

    assert result.exit_code == 0, result.output
    assert len(seen) == 2, f"expected two poll passes, got {len(seen)}"
    assert _CountingEmbedder.constructions == 1, (
        "expected ONE FastEmbedEmbedder for the whole process, got "
        f"{_CountingEmbedder.constructions}"
    )
    assert seen[0] is not None, "work() did not pass its embedder into drain()"
    assert seen[0] is seen[1], "each poll pass got a different embedder instance"


def _patch_cli_clock_past_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake ``lode.cli``'s clock so ``work --wait``'s deadline trips on the first check.

    ``work()``'s ``deadline = time.monotonic() + timeout_s`` is set by whichever call to
    ``time.monotonic()`` happens first; every later call must read past that deadline so
    the loop's first timeout check trips immediately. A CONSTANT fake clock is wrong here:
    the deadline calc and the check would then read the identical value, so
    ``now >= deadline`` (now == deadline - timeout_s) would never hold and the loop would
    spin for real -- sleeping ``--interval`` seconds -- forever.

    A two-value counter (0.0 on call #1, a huge constant after) was tried and rejected
    (lode-e8lo): it is order-proof only by luck, because ``work()`` happens to call
    ``time.monotonic()`` for the deadline before anything else reachable from it does. A
    ``time.monotonic()`` added anywhere upstream of that read -- e.g. inside
    ``cli._resolve_settings()`` (``src/lode/cli/__init__.py``, called before the deadline calc, in
    ``cli``'s own namespace) -- would consume call #1 itself, making call #2 the deadline
    instead; the counter's "call #1 is special" premise would then be exactly backwards,
    and the loop would spin forever rather than time out.

    This clock is order-proof BY CONSTRUCTION instead: an unboundedly advancing source
    whose step exceeds ``Settings.work_wait_timeout_s`` (default 1800,
    ``src/lode/config.py``). Whichever call establishes the deadline, the very next call
    is guaranteed to read at least one full ``step`` past it -- and ``step`` alone already
    exceeds the timeout -- so the first read after the deadline is set always trips the
    check. That holds no matter how many ``time.monotonic()`` calls (from ``cli`` or
    anywhere else reachable through it, now or in the future) precede the deadline read; no
    call-ordering assumption remains, and the counting predicate is gone.

    The step is a bare literal, deliberately NOT derived from the setting -- and what that
    decoupling costs is bounded rather than fatal, so raising the setting past it is not a
    trap: readings climb one ``step`` per call unconditionally, so the check trips within
    ``ceil(work_wait_timeout_s / step)`` passes for ANY pair of values, and only a
    NON-advancing clock can spin. The magnitude buys SPEED (trip on the *first* check), not
    termination. Measured during lode-e8lo's review: with ``work_wait_timeout_s`` raised to
    2_500_000 both tests below still pass, taking three loop passes instead of one.

    This rebinds the *name* ``time`` inside ``lode.cli``; it never sets an attribute on
    the shared ``time`` module object, so no other module observes this fake. What that
    narrowed exposure costs this suite is owned by ``tests/conftest.py``'s
    ``_reset_jobs_clock_anchor`` (lode-x10m, lode-e8lo). A ``time`` member this namespace
    omits raises ``AttributeError``, which surfaces as a failed ``result.stderr``
    assertion in both callers below rather than as a hang.
    """
    clock = itertools.count(0.0, 1_000_000.0)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _seconds: None),
    )


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
                "INSERT INTO jobs (type, target_version, next_attempt_at) "
                "VALUES (?, ?, ?)",
                ("refresh", "ver-stuck", now_iso()),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    _patch_cli_clock_past_deadline(monkeypatch)

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
                "INSERT INTO jobs (type, target_version, next_attempt_at) "
                "VALUES (?, ?, ?)",
                ("refresh", "ver-stuck", now_iso()),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "drained 0 job(s)" in result.stdout


def test_work_one_shot_reports_outstanding_jobs_not_just_drained_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot pass over a thrashing head is no longer silent about it (lode-olmi.13).

    A 'refresh' job has no registered handler, so it stays 'pending' across a
    single one-shot pass -- the same shape as a reconcile re-enqueue that
    drain() never reaches this pass. Plain 'lode work' (no --wait) must name
    it explicitly instead of just printing 'drained 0 job(s)' and exiting
    silent about what's left.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version, next_attempt_at) "
                "VALUES (?, ?, ?)",
                ("refresh", "ver-stuck", now_iso()),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "drained 0 job(s)" in result.stdout
    assert "1 job(s) still outstanding after this pass" in result.stdout
    assert "refresh" in result.stdout
    assert "pending" in result.stdout


def test_work_wait_does_not_duplicate_the_one_shot_outstanding_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'--wait' keeps its own outstanding-jobs reporting -- no duplicate generic line.

    --wait already decides whether to keep polling from jobs_read.outstanding_jobs()
    and names outstanding jobs itself on timeout; the new one-shot/--loop
    "still outstanding after this pass" line (lode-olmi.13) is specific to
    the non---wait path and must not also appear under --wait.
    """
    import lode.worker as worker_mod

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (type, target_version, next_attempt_at) "
                "VALUES (?, ?, ?)",
                ("refresh", "ver-stuck", now_iso()),
            )
    finally:
        conn.close()

    monkeypatch.setattr(worker_mod, "_REGISTRY", _noop_embed_registry())

    _patch_cli_clock_past_deadline(monkeypatch)

    result = runner.invoke(app, ["work", "--db", str(db_path), "--wait"])
    assert result.exit_code == 1
    assert "timed out" in result.stderr
    assert "still outstanding after this pass" not in result.stdout
    assert "still outstanding after this pass" not in result.stderr


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
    dt = datetime.now(UTC) - timedelta(seconds=seconds_ago)
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
                "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
                "VALUES ('embed', 'snap-1', 'done', ?)",
                (now_iso(),),
            )
            conn.execute(
                "INSERT INTO jobs (type, target_version, status, next_attempt_at) "
                "VALUES ('enrich', 'snap-1', 'done', ?)",
                (now_iso(),),
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
    # `add` runs the enrich leg inline; keep it offline (patch at the real
    # lookup site -- lode.cli has no enrich_version symbol of its own, lode-8xg).
    _noop_enrich(monkeypatch)

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


# --- lode models pull (explicit model-weights prefetch, lode-6qh/lode-og3) ---
#
# The download itself is opt-in/live-only (tests/test_models_smoke.py,
# LODE_SMOKE_MODELS=1) -- these tests keep the default gate offline by faking
# the three lazy-load wrapper classes 'models pull' constructs
# (lode.embedding.FastEmbedEmbedder, lode.retrieval.FastEmbedCrossEncoder,
# lode.faithfulness.FastEmbedEntailmentScorer), asserting only that the right
# ones are warmed -- never that fastembed/HuggingFace is actually reached.


def _install_fake_model_loaders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fake out the three FastEmbed*-wrapper classes 'models pull' constructs.

    Each fake's ``warm()`` just records a distinguishable tag instead of
    constructing a real ``fastembed`` model -- offline proof of which models
    were (or weren't) warmed, and in what order. Faking the *public* ``warm()``
    (not the private ``_load()`` it delegates to) is deliberate: it pins the
    seam the CLI actually depends on, so renaming the private loader can never
    leave these tests green while ``lode models pull`` dies on an AttributeError.
    """
    calls: list[str] = []

    def _fake(tag: str) -> type:
        class _FakeLoader:
            def __init__(self, settings: object) -> None:
                pass

            def warm(self) -> None:
                calls.append(tag)

        return _FakeLoader

    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _fake("embedder"))
    monkeypatch.setattr("lode.retrieval.FastEmbedCrossEncoder", _fake("reranker"))
    monkeypatch.setattr(
        "lode.faithfulness.FastEmbedEntailmentScorer", _fake("entailment")
    )
    return calls


def test_models_pull_warms_both_models_and_reports_where_they_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'lode models pull' warms the embedder + reranker and says where they went.

    Acceptance (lode-6qh): one command warms both model caches from a cold start
    and reports where they went ($LODE_HOME/models). Default settings pin
    entailment_model == rerank_model (lode-txh.6), so the entailment scorer is a
    same-model cache hit -- surfaced in the output, not loaded a second time and
    not silently skipped.
    """
    calls = _install_fake_model_loaders(monkeypatch)
    home = tmp_path / "home"

    result = runner.invoke(app, ["models", "pull"], env={"LODE_HOME": str(home)})

    assert result.exit_code == 0, result.output
    assert calls == ["embedder", "reranker"]
    assert str(home / "models") in result.stdout
    assert "entailment" in result.stdout
    assert "already cached" in result.stdout


def test_models_pull_warms_entailment_separately_when_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.toml override that splits entailment_model from rerank_model
    warms both, driven through a REAL $LODE_HOME/config.toml (the lode-40g
    pattern, e.g. test_work_honors_config_file_refresh_ttl_s_end_to_end above)
    -- not a 'lode.cli.Settings' monkeypatch. This is the lode-og3 rebuild's
    load-bearing test: it proves the override reaches 'models pull' via
    _resolve_settings() and would fail if models_pull ever reverts to a bare
    Settings() (lode-cpf's bounce finding against the original lode-6qh
    branch), since a bare Settings() would silently ignore this file and warm
    only the pinned default (same model for both, so 'entailment' would never
    be warmed a second time).
    """
    calls = _install_fake_model_loaders(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'entailment_model = "some-other/nli-model"\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["models", "pull"], env={"LODE_HOME": str(home)})

    assert result.exit_code == 0, result.output
    assert calls == ["embedder", "reranker", "entailment"]


def test_models_pull_honors_config_file_embedding_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.toml embedding_model override is the model ACTUALLY warmed --
    not the pinned default (lode-cpf / lode-og3).

    Regression coverage for the exact bounce finding against the original
    lode-6qh branch: it constructed a bare 'Settings()', so 'models pull'
    would warm 'nomic-ai/nomic-embed-text-v1.5' (the pinned default)
    regardless of what the user's config.toml named, while 'lode work'/
    'lode ask' (which DO resolve settings via _resolve_settings()) would still
    warm/download the user's actual configured model mid-capture -- the exact
    surprise phone-home this command exists to eliminate. Asserting on the
    printed 'embedder: <model>' line (not just exit_code) is the point: it
    proves the overridden id, not the default, is what 'models pull' reports
    (and therefore warms).
    """
    _install_fake_model_loaders(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    overridden_model = "some-other/embedding-model"
    (home / "config.toml").write_text(
        f'embedding_model = "{overridden_model}"\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["models", "pull"], env={"LODE_HOME": str(home)})

    assert result.exit_code == 0, result.output
    assert f"embedder: {overridden_model}" in result.stdout
    assert "nomic-ai/nomic-embed-text-v1.5" not in result.stdout


def test_models_pull_reports_a_bad_config_file_without_a_traceback(
    tmp_path: Path,
) -> None:
    """A malformed config.toml is a clean CLI error, not a crash (lode-cpf).

    Mirrors test_cli_reports_a_bad_config_file_without_a_traceback (lode-40g)
    for 'models pull' specifically: _resolve_settings() (not a bare
    Settings(), which never reads the file and so could never fail this way)
    converts an unusable $LODE_HOME/config.toml into the one-line stderr +
    exit-1 convention every other command uses.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("embedding_model =\n", encoding="utf-8")

    result = runner.invoke(app, ["models", "pull"], env={"LODE_HOME": str(home)})

    assert result.exit_code == 1, result.output
    assert "invalid config file" in result.stderr
    assert str(home / "config.toml") in result.stderr
    # The load-bearing assertion, same reasoning as the lode-40g test: proves
    # _resolve_settings() caught it (typer.Exit / SystemExit) rather than the
    # raw TOMLDecodeError escaping to the terminal.
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_models_pull_is_listed_under_models_help() -> None:
    """'lode models --help' lists 'pull' -- the sub-app group is real, not a stub."""
    result = runner.invoke(app, ["models", "--help"])
    assert result.exit_code == 0
    assert "pull" in result.stdout


@pytest.mark.real_embedder
def test_real_model_wrappers_expose_warm() -> None:
    """The three real wrappers expose the public warm() seam 'models pull' calls.

    The tests above fake the wrapper classes wholesale, so they would stay green
    even if the real classes lost warm() -- this pins the contract against the
    genuine articles. Constructing a wrapper only stores the model name (no
    fastembed import, no download), so this stays offline.

    ``@pytest.mark.real_embedder`` is load-bearing HERE for a reason worth stating,
    because the marker's usual justification does not apply and the omission is
    silent (lode-sx17 land-review): the import below is *function-local*, so it
    reads ``lode.embedding``'s attribute at CALL time -- which the autouse offline
    stub has replaced (lode-7ypf). Without the marker this asserts ``callable`` on
    a ``warm()`` that tests/conftest.py wrote, i.e. it becomes precisely the
    fake-wrapper vacuity the docstring above says it exists to prevent, while
    still passing. The other two wrappers are unaffected (nothing stubs them), so
    only the ``FastEmbedEmbedder`` leg was hollow -- which is exactly why nothing
    failed to give it away.
    """
    from lode.embedding import FastEmbedEmbedder
    from lode.faithfulness import FastEmbedEntailmentScorer
    from lode.retrieval import FastEmbedCrossEncoder

    settings = load_settings()
    for wrapper in (
        FastEmbedEmbedder(settings),
        FastEmbedCrossEncoder(settings),
        FastEmbedEntailmentScorer(settings),
    ):
        assert callable(wrapper.warm)


# --- lode-96t: 'models pull''s most likely failure path is a clear message, ---
# --- not a raw fastembed/huggingface_hub traceback.                        ---
#
# fastembed's actual behavior (verified empirically against the installed
# package, not just read from source) collapses more than the source alone
# suggests: a genuine HTTP error (rate-limit/5xx) and HF_HUB_OFFLINE=1 against a
# cold cache both end up as the *same* generic
# ValueError("Could not load model {id} from any source.") -- fastembed's retry
# loop swallows HfHubHTTPError/LocalEntryNotFoundError/RepositoryNotFoundError
# internally and never re-raises the original cause. Only a total network
# failure (DNS/connect/timeout) escapes fastembed uncaught, as an
# httpx.TransportError. These tests fake exactly one wrapper's warm() to raise
# the specific exception each real failure mode produces (never a real network
# call), and assert 'lode models pull' maps each to its own actionable message
# -- while a ValueError that doesn't carry fastembed's signature, or any other
# exception entirely, still propagates raw (the no-broad-except-Exception
# constraint the design explicitly calls for).


def _install_failing_embedder(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """Fake FastEmbedEmbedder.warm() to raise ``exc`` instead of downloading.

    The embedder is warmed first in 'models pull', so a raised exception here
    never reaches the reranker/entailment warms -- exactly like the CLI's own
    try/except boundary sees it.
    """

    class _FailingLoader:
        def __init__(self, settings: object) -> None:
            pass

        def warm(self) -> None:
            raise exc

    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _FailingLoader)


def test_models_pull_no_network_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No network reachable at all -> a clear message and exit 1, not a traceback.

    fastembed's retry loop only catches (EnvironmentError, RepositoryNotFoundError,
    ValueError) -- none of which an httpx transport failure subclasses -- so this
    is the one failure mode that escapes fastembed as-is, verified empirically
    against an unreachable HF_ENDPOINT.
    """
    import httpx

    _install_failing_embedder(
        monkeypatch, httpx.ConnectError("[Errno -2] Name or service not known")
    )

    result = runner.invoke(app, ["models", "pull"])

    assert result.exit_code == 1, result.output
    assert "could not reach huggingface" in result.stderr.lower(), result.stderr
    assert "no network route" in result.stderr.lower(), result.stderr
    # The load-bearing assertion (mirrors the config-file test's pattern): proves
    # _warm() caught it (typer.Exit / SystemExit) rather than the raw
    # httpx.ConnectError escaping to the terminal.
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_models_pull_offline_cold_cache_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF_HUB_OFFLINE=1 against a cold cache -> exits explaining cache + flag.

    Verified empirically: under HF_HUB_OFFLINE=1, fastembed forces
    local_files_only=True throughout and never attempts the network at all, so a
    cold-cache miss surfaces as fastembed's generic
    ValueError("Could not load model {id} from any source.") with no distinct
    exception type of its own -- the only way to tell this apart from a genuine
    HTTP error (below) is the env var lode already knows about.
    """
    _install_failing_embedder(
        monkeypatch,
        ValueError("Could not load model BAAI/bge-small-en-v1.5 from any source."),
    )

    result = runner.invoke(app, ["models", "pull"], env={"HF_HUB_OFFLINE": "1"})

    assert result.exit_code == 1, result.output
    assert "cache is cold" in result.stderr.lower(), result.stderr
    assert "hf_hub_offline" in result.stderr.lower(), result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_models_pull_http_error_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine HuggingFace HTTP error (rate-limit/5xx) -> its own clear message.

    Same underlying ValueError as the offline/cold-cache case above (fastembed
    swallows the HfHubHTTPError before it ever reaches us) -- distinguished only
    by HF_HUB_OFFLINE being unset here, proving the two really do produce
    different, non-overlapping messages despite sharing an exception type.

    lode-4hy1: a GCS-mirrored model's exhausted-mirror failure collapses into
    this same ValueError too -- fastembed swallows that leg in a bare
    ``except Exception`` (see :func:`lode.cli._warm`'s docstring) -- so the
    message must name the mirror as a possible cause rather than blaming
    HuggingFace alone.
    """
    _install_failing_embedder(
        monkeypatch,
        ValueError("Could not load model BAAI/bge-small-en-v1.5 from any source."),
    )

    result = runner.invoke(app, ["models", "pull"])

    assert result.exit_code == 1, result.output
    assert "failed to download" in result.stderr.lower(), result.stderr
    assert "rate-limiting or unavailable" in result.stderr.lower(), result.stderr
    # lode-4hy1 (the substance, not the exact copy): the mirror is named as a
    # possible cause, so the message never pins the blame solely on HuggingFace.
    assert "gcs mirror" in result.stderr.lower(), result.stderr
    # Distinct from the offline/cold-cache message above.
    assert "hf_hub_offline" not in result.stderr.lower(), result.stderr
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def test_models_pull_unexpected_exception_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine bug is never read as a network problem (the design's core ask).

    No broad `except Exception` -- an exception type _warm() doesn't map at all
    escapes exactly as raised, proving the mapping is scoped to the specific
    fastembed/huggingface_hub failure modes, not a catch-all.
    """
    _install_failing_embedder(monkeypatch, RuntimeError("a genuine defect"))

    result = runner.invoke(app, ["models", "pull"])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError), repr(result.exception)
    assert str(result.exception) == "a genuine defect"


def test_models_pull_unmatched_value_error_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError that doesn't carry fastembed's exhausted-sources signature
    still propagates -- the message match is deliberately narrow, not a bare
    `except ValueError` that would also swallow an unrelated bug.
    """
    _install_failing_embedder(monkeypatch, ValueError("some unrelated ValueError"))

    result = runner.invoke(app, ["models", "pull"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError), repr(result.exception)
    assert str(result.exception) == "some unrelated ValueError"


@pytest.mark.real_embedder
def test_fastembed_still_raises_the_exhausted_sources_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canary: the REAL fastembed still raises the message ``_warm()`` keys off.

    Every other lode-96t test *fakes* fastembed's
    ``ValueError("Could not load model ... from any source.")``, so all of them
    would stay green if a fastembed upgrade reworded that string -- while
    ``lode models pull`` silently regressed to the raw traceback this ticket
    exists to remove (the unmatched ValueError would simply propagate). That
    string is the only signature fastembed leaves us to key off
    (:data:`lode.cli._FASTEMBED_EXHAUSTED_SOURCES` -- it catches
    ``HfHubHTTPError``/``LocalEntryNotFoundError`` internally and never chains
    the cause), so pin it against the *installed* package: an upgrade that
    rewords it fails here, loudly, instead of in a user's terminal.

    Hermetic and offline: ``HF_HUB_OFFLINE=1`` against a cold ``$LODE_HOME``
    makes fastembed force ``local_files_only=True`` throughout, so it never
    touches the network -- it exhausts its sources locally and raises.

    ``@pytest.mark.real_embedder`` (lode-7ypf) opts out of tests/conftest.py's
    autouse offline embedder stub -- this test is the one place in the suite
    that is *about* the installed package's own behaviour, so a stand-in would
    make it assert on a string this repo wrote. It deliberately does NOT reach
    for ``network``/``slow``: the socket guard stays on, because the offline
    claim above is part of what is being asserted.
    """
    from lode.cli import _FASTEMBED_EXHAUSTED_SOURCES
    from lode.embedding import FastEmbedEmbedder

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("LODE_HOME", str(tmp_path))

    with pytest.raises(ValueError) as excinfo:
        FastEmbedEmbedder(load_settings()).warm()

    assert _FASTEMBED_EXHAUSTED_SOURCES in str(excinfo.value), (
        "fastembed reworded its exhausted-sources error; lode.cli._warm() no "
        f"longer recognizes it and 'lode models pull' will traceback: {excinfo.value!r}"
    )


def test_huggingface_hub_still_declares_httpx_as_its_transport() -> None:
    """Canary: ``_warm()``'s ``except httpx.TransportError`` arm still has a
    library underneath it that can actually raise one (lode-iadh).

    The ValueError canary above pins fastembed's exhausted-sources *message*;
    this pins the other arm's *exception type*, which is a transitive coupling
    rather than a direct one: fastembed itself declares ``requests``, not
    ``httpx`` (``pip show fastembed`` / its own dependency metadata never
    mentions httpx). The ``httpx.TransportError`` ``_warm()`` catches is raised
    by **huggingface_hub**, which fastembed delegates model downloads to.

    The obvious cheap guard -- "if the transport changes, our own `import
    httpx` breaks" -- does NOT work: httpx is a *direct* lode dependency
    (``pyproject.toml``, for the unrelated web draw-down client) and is also
    required independently by ``anthropic``, so ``import httpx`` keeps
    succeeding no matter what fastembed/huggingface_hub do. There is no
    ImportError to key off.

    So this pins the actual coupling via package metadata instead of behavior:
    if huggingface_hub ever drops httpx for another transport (it has swapped
    transports once before, requests -> httpx), this fails here, loudly and
    hermetically -- no network, no loopback port, nothing flaky -- rather than
    ``except httpx.TransportError`` silently stopping matching and 'lode
    models pull' regressing to a raw traceback on its single most likely
    failure path (no network), with the rest of the suite staying green.

    Scope, stated plainly: this pins huggingface_hub's *declared* core
    dependency, not its *runtime* behavior. It catches the realistic regression
    -- a transport swap, which necessarily changes the dependency -- but not the
    exotic one where huggingface_hub keeps declaring httpx and raises something
    else anyway. A behavioral canary (point HF's endpoint at a refused loopback
    port) was deliberately rejected in lode-og3 review as version-sensitive and
    flaky, and a flaky canary is worse than none.

    Note the ``extra`` filter below is load-bearing, not decoration:
    huggingface_hub lists httpx *five* times -- once as a core dependency and
    four more under the ``oauth``/``testing``/``all``/``dev`` extras. A naive
    substring match over the raw list would keep passing if huggingface_hub
    moved to another core transport while merely retaining httpx as a test
    extra, which is exactly the shape a real transport migration takes. That
    would leave this canary green while the arm it guards was already dead.
    """
    from importlib.metadata import requires

    from packaging.requirements import Requirement

    hub_requires = [Requirement(r) for r in requires("huggingface_hub") or []]
    # Core dependencies only -- an ``extra == ...`` requirement is optional, so
    # it proves nothing about the transport huggingface_hub actually uses.
    core_requires = [
        req for req in hub_requires if not (req.marker and "extra" in str(req.marker))
    ]
    assert any(req.name == "httpx" for req in core_requires), (
        "huggingface_hub no longer declares httpx as a core dependency (core "
        f"deps are now: {sorted(req.name for req in core_requires)!r}). "
        "lode.cli._warm() catches httpx.TransportError to turn the no-network "
        "case into 'no network route to huggingface.co' -- and that arm only "
        "matches because fastembed delegates its downloads to huggingface_hub "
        "(fastembed itself never declared httpx). If the hub has switched "
        "transports, that arm is dead and 'lode models pull' now tracebacks on "
        "the no-network path instead. TO FIX: catch the new transport's "
        "exception in _warm() (src/lode/cli/models.py), and re-point this canary at "
        "the new dependency."
    )

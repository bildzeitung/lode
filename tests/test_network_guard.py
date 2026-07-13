"""Regression tests for the autouse network/LLM-client guard (lode-85q).

Surfaced by lode-8xg: a test whose mock was silently a no-op still reached the
*real* ``enrich_version`` -> ``anthropic.Anthropic`` path, and the test still
passed -- in an unkeyed environment ``anthropic.Anthropic()`` raises at
construction (before any socket opens) and ``lode.worker.run_one``'s
``except Exception`` swallows that as an ordinary job failure; in a keyed
environment it would instead make a live, billed call. Neither world reported
"this test touched the network".

These tests exercise ``tests/conftest.py``'s ``_block_unmocked_network_and_llm_access``
fixture directly -- none of them carry ``@pytest.mark.network``, so the guard is
active for every test here, exactly as it is for the rest of the suite.
"""

import socket
import sqlite3
from pathlib import Path

import anthropic
import pytest

from lode.config import Settings
from lode.storage import init_db
from lode.worker import claim_and_run_one


def test_real_anthropic_construction_fails_loudly() -> None:
    """An un-mocked ``anthropic.Anthropic()`` fails with a message naming it."""
    with pytest.raises(pytest.fail.Exception, match="anthropic.Anthropic"):
        anthropic.Anthropic()


def test_real_outbound_socket_connect_fails_loudly() -> None:
    """A real, non-loopback connect attempt fails with a message naming it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(pytest.fail.Exception, match="outbound network connection"):
            sock.connect(("93.184.216.34", 443))  # example.com's IP -- never dialed
    finally:
        sock.close()


def test_real_outbound_connect_ex_fails_loudly() -> None:
    """``connect_ex`` is guarded too -- it does not route through ``connect``.

    Guarding only ``socket.connect`` left the guard failing *open* here: a
    ``connect_ex`` to a public IP sailed straight through. A guard that
    silently misses is worse than no guard, so both are patched.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(pytest.fail.Exception, match="outbound network connection"):
            sock.connect_ex(("93.184.216.34", 443))  # example.com's IP -- never dialed
    finally:
        sock.close()


def test_loopback_alias_in_127_block_is_permitted() -> None:
    """The whole ``127.0.0.0/8`` block is loopback, not just ``127.0.0.1``.

    ``127.0.1.1`` is the stock Debian/Ubuntu ``/etc/hosts`` alias for the
    machine's own hostname; a string-equality allowlist wrongly failed it as
    "outbound network".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):  # refused, not guard-failed
            sock.connect(("127.0.1.1", 1))
    finally:
        sock.close()


def test_unix_socket_connect_is_not_treated_as_egress(tmp_path: Path) -> None:
    """An ``AF_UNIX`` connect cannot reach a remote host -- blocking it is a
    pure false positive, and a baffling one (the message says "network")."""
    sock_path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        server.listen(1)
        client.connect(str(sock_path))  # must not raise
    finally:
        client.close()
        server.close()


def test_loopback_connect_is_still_permitted() -> None:
    """The loopback escape stays intact for tests/test_webfetch.py's pattern.

    A refused connection to a loopback port must still surface as a genuine
    ``ConnectionRefusedError`` (or an equivalent OS error), not our guard's
    ``pytest.fail`` -- this is exactly what
    ``TestHttpxFetcher.test_connection_error_is_transient`` relies on.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            sock.connect(("127.0.0.1", 1))
    finally:
        sock.close()


@pytest.fixture()
def _enrich_job(tmp_path: Path) -> tuple[sqlite3.Connection, Path, int]:
    """A real, live, egress-eligible note version plus a pending ``enrich`` job.

    No fake client or handler is installed anywhere -- ``claim_and_run_one``
    below dispatches through the module-level, real ``_REGISTRY`` (the
    production ``_enrich_handler`` -> ``enrich_version`` -> ``build_client``
    path), exactly mirroring how lode-8xg's broken mock silently fell through.
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO notes (note_id, head_version_id, no_egress) VALUES ('n1', 'v1', 0)"
    )
    conn.execute(
        "INSERT INTO versions (version_id, note_id, body, op) "
        "VALUES ('v1', 'n1', 'a note with nothing sensitive in it', 'create')"
    )
    conn.execute(
        "INSERT INTO jobs (type, target_version, status, attempts, next_attempt_at) "
        "VALUES ('enrich', 'v1', 'pending', 0, '2000-01-01T00:00:00.000Z')"
    )
    conn.commit()
    try:
        yield conn, db_path, 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "ambient_key",
    [None, "sk-ant-fake-ambient-key"],
    ids=["unkeyed", "keyed"],
)
def test_unmocked_enrich_job_fails_loudly_not_swallowed(
    _enrich_job: tuple[sqlite3.Connection, Path, int],
    monkeypatch: pytest.MonkeyPatch,
    ambient_key: str | None,
) -> None:
    """The acceptance bar (bd show lode-85q): FAILS in both a keyed and an
    unkeyed environment, with a message naming the client access -- never a
    quietly-recorded ``status='failed'`` job row that leaves the test green.

    ``worker.run_one``'s ``except Exception`` (a legitimate, broad job-failure
    handler, left alone here -- lode-85q explicitly scopes fixing it out) would
    ordinarily swallow this; the guard's ``pytest.fail()`` raises a
    ``BaseException`` subclass that blows straight through it regardless of
    whether ``ANTHROPIC_API_KEY`` happens to be set.
    """
    if ambient_key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_key)

    conn, db_path, _job_id = _enrich_job

    with pytest.raises(pytest.fail.Exception, match="anthropic.Anthropic"):
        claim_and_run_one(conn, db_path, Settings(), types=("enrich",))

    # The job must NOT have been silently recorded as an ordinary failure --
    # the whole point is that this never reaches run_one's except Exception.
    (status,) = conn.execute(
        "SELECT status FROM jobs WHERE target_version = 'v1'"
    ).fetchone()
    assert status == "running", (
        "job status advanced past 'running' -- the real client construction "
        "was caught by run_one's except Exception instead of blowing through it"
    )

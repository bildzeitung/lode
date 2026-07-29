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

Every test that deliberately trips a guard and catches the raise carries
``@pytest.mark.trips_network_guard`` (lode-sx17), which consumes the violation
the guard records. **That marker is also the sabotage recipe for the teardown
backstop**: delete it from any one test below and that test must start failing
in teardown with "no failure reached pytest" -- if it still passes, the backstop
has become a no-op and the guard is back to resting on ``pytest.fail`` raising a
``BaseException`` that no caller happens to swallow.
"""

import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import anthropic
import pytest
from conftest import _unconsumed_violations_message

from lode.config import Settings
from lode.storage import init_db
from lode.worker import claim_and_run_one

_TESTS_DIR = Path(__file__).resolve().parent


@pytest.mark.trips_network_guard
def test_real_anthropic_construction_fails_loudly() -> None:
    """An un-mocked ``anthropic.Anthropic()`` fails with a message naming it."""
    with pytest.raises(pytest.fail.Exception, match="anthropic.Anthropic"):
        anthropic.Anthropic()


@pytest.mark.trips_network_guard
def test_real_outbound_socket_connect_fails_loudly() -> None:
    """A real, non-loopback connect attempt fails with a message naming it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(pytest.fail.Exception, match="outbound network connection"):
            sock.connect(("93.184.216.34", 443))  # example.com's IP -- never dialed
    finally:
        sock.close()


@pytest.mark.trips_network_guard
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


@pytest.mark.trips_network_guard
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


# --- lode-sx17: the guard no longer rests on nobody swallowing the raise ----


@pytest.mark.trips_network_guard
def test_a_swallowed_guard_failure_is_still_recorded(
    guard_violations: list[str],
) -> None:
    """The record survives a caller that swallows the raise *completely*.

    This is the property the whole backstop rests on. The ``except
    BaseException`` below is deliberately broader than anything a real
    dependency would write -- ``huggingface_hub``'s registry fetch, the site
    that prompted this (lode-sx17), swallows only ``Exception``, which
    ``pytest.fail``'s ``Failed`` happens to clear. Testing against the *widest*
    swallow is what makes the assertion mean "no caller can hide this",
    rather than "no caller we know of hides this".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.connect(("93.184.216.34", 443))  # example.com's IP -- never dialed

        # under test, so ruff's "don't do this" is exactly what to do here.
        except BaseException:  # noqa: S110
            pass
    finally:
        sock.close()

    assert len(guard_violations) == 1, (
        "the guard blocked the connect but recorded nothing -- with the raise "
        "swallowed there is now no signal at all"
    )
    assert "93.184.216.34" in guard_violations[0]
    assert "intercepted at:" in guard_violations[0], (
        "the record must carry the stack captured at interception -- in the "
        "swallow and off-main-thread cases it is the only thing naming the caller"
    )


def test_a_clean_test_records_nothing(guard_violations: list[str]) -> None:
    """Non-vacuity for the test above: an ordinary test leaves the list empty.

    Without this, ``len(...) == 1`` would still pass on a guard that recorded
    unconditionally, and the teardown backstop would fail every test in the
    suite.
    """
    assert guard_violations == []


@pytest.mark.parametrize(
    ("recorded", "expected", "wanted"),
    [
        ([], False, None),
        (["a violation"], False, "no failure reached pytest"),
        (["a violation"], True, None),
        ([], True, "the marker is stale"),
    ],
    ids=["clean", "swallowed", "deliberate", "stale-marker"],
)
def test_unconsumed_violations_verdict(
    recorded: list[str], expected: bool, wanted: str | None
) -> None:
    """The teardown verdict, unit-tested as a pure function.

    Split out of the fixture for the same reason ``_wrong_source_tree_message``
    is (see its docstring): a fixture's teardown can otherwise only be
    exercised by running pytest under pytest -- which the end-to-end test below
    does do, once, for the wiring.
    """
    message = _unconsumed_violations_message(recorded, expected=expected)
    if wanted is None:
        assert message is None
    else:
        assert message is not None and wanted in message


def test_hub_telemetry_is_disabled_and_skips_the_agent_registry_fetch() -> None:
    """``HF_HUB_DISABLE_TELEMETRY`` is set early enough to actually take effect.

    Two assertions, and both are needed:

    * The **constant**, not the env var. ``huggingface_hub.constants`` freezes
      the environment into a module constant at import, so a ``True`` here is
      the only proof that tests/conftest.py's assignment ran *before* anything
      imported the hub. Asserting ``os.environ`` instead would pass even if the
      constant had already frozen at ``False`` -- the exact failure mode the
      ordering constraint in that comment warns about.
    * The **behaviour**, not just the flag. ``detect_agent()`` -- whose cold
      cache fetches the harness registry over the network -- must not be
      reached while building request headers. Patching it to blow up is what
      makes this fail if a future huggingface_hub moves the telemetry check,
      renames the var, or starts calling it from a second site.

    Residual, accepted: an ambient ``HF_HUB_DISABLE_TELEMETRY``/
    ``DISABLE_TELEMETRY``/``DO_NOT_TRACK`` in the developer's own shell would
    satisfy both without conftest's line. Nothing here can tell the two apart
    (by the time this runs the constant is frozen either way), and the test is
    still worth having: on CI, and on any shell without those set, it is the
    only thing that catches a broken ordering.
    """
    from huggingface_hub import constants
    from huggingface_hub.utils import _headers

    assert constants.HF_HUB_DISABLE_TELEMETRY is True, (
        "huggingface_hub was imported before tests/conftest.py set "
        "HF_HUB_DISABLE_TELEMETRY -- the constant is frozen at import, so the "
        "agent-harness registry fetch is live again (lode-sx17)"
    )

    def _must_not_be_called() -> str:
        raise AssertionError(
            "building Hub request headers called detect_agent() despite "
            "HF_HUB_DISABLE_TELEMETRY -- the registry fetch is reachable again"
        )

    # Driven through the PUBLIC build_hf_headers() -- the entry point every Hub
    # request actually goes through -- rather than the private _http_user_agent()
    # it delegates to, so this keeps exercising the real path if that private
    # helper is renamed again (it already has been).
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_headers, "detect_agent", _must_not_be_called)
        headers = _headers.build_hf_headers()

    assert "agent/" not in headers["user-agent"]


#: A two-test session run under the real conftest by the subprocess below. The
#: first test must pass clean; the second blocks a connect and then swallows the
#: raise whole, which only the teardown backstop can turn back into a failure.
_TEARDOWN_BACKSTOP_SESSION = '''
"""Generated by tests/test_network_guard.py -- not collected by the suite."""

import socket


def test_clean_test_is_untouched_by_the_backstop() -> None:
    assert True


def test_swallowed_egress_is_caught_in_teardown() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.connect(("93.184.216.34", 443))
        except BaseException:
            pass
    finally:
        sock.close()
'''


def test_swallowed_violation_fails_the_run_in_teardown(tmp_path: Path) -> None:
    """End-to-end: the wiring, not just the verdict function (lode-sx17).

    The pure-function test above proves the *decision*; this proves it is
    actually reached -- that the autouse fixture's teardown runs the check and
    that ``pytest.fail`` there really does fail the run. A fixture teardown
    cannot be exercised any other way than by running pytest under pytest, so
    this pays for one subprocess session.

    tests/conftest.py is loaded with ``-p conftest`` (PYTHONPATH points at
    tests/) rather than copied next to the generated file, which matters twice
    over: the REAL conftest is what is under test, and its ``_CHECKOUT_ROOT``
    stays derived from its own ``__file__``, so guard 0's source-tree check
    still sees this checkout instead of rejecting ``tmp_path``.

    Both outcomes are asserted in one session. The clean test passing is the
    non-vacuity leg: a backstop that failed everything would satisfy the error
    assertion alone.
    """
    session = tmp_path / "test_generated_backstop_session.py"
    session.write_text(_TEARDOWN_BACKSTOP_SESSION, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(session), "-p", "conftest", "-v"],
        capture_output=True,
        text=True,
        check=False,  # a non-zero exit is the expected outcome, not an error
        env={**os.environ, "PYTHONPATH": str(_TESTS_DIR)},
        cwd=tmp_path,
        timeout=300,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"the swallowed egress did not fail the run:\n{output}"
    )
    assert "test_clean_test_is_untouched_by_the_backstop PASSED" in output, (
        f"the clean test did not pass -- the backstop is failing everything:\n{output}"
    )
    assert "test_swallowed_egress_is_caught_in_teardown" in output
    assert "no failure reached pytest" in output, (
        f"the run failed, but not with the backstop's message:\n{output}"
    )
    assert "93.184.216.34" in output, (
        f"the failure did not carry the recorded violation:\n{output}"
    )

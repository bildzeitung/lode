"""Offline tests for scripts/measure_vectorstore_session.py (lode-3gr3).

No gate runs the script, and ``docs/decisions.md`` records figures from it by mode. These
pin what each mode says it measures: how often the writer reconnects, which connections
get a capped ``Session``, and that the reads do not run on the writer's connection.
"""

from pathlib import Path
from typing import Any

import lancedb
import pytest
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

_mod = load_module_from_path(
    "measure_vectorstore_session",
    REPO_ROOT / "scripts" / "measure_vectorstore_session.py",
)
runner = CliRunner()


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The ``session=`` argument of every ``lancedb.connect`` call, in order."""
    seen: list[Any] = []
    real_connect = lancedb.connect

    def connect(uri: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("session"))
        return real_connect(uri, **kwargs)

    monkeypatch.setattr(lancedb, "connect", connect)
    return seen


def _run(*args: str) -> str:
    result = runner.invoke(_mod.app, ["--rate", "0", *args])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize(
    ("mode", "connects", "capped"),
    [("reconnect", 2, False), ("held", 1, False), ("capped", 1, True)],
)
def test_mode_sets_the_writers_connections(
    mode: str, connects: int, capped: bool, sessions: list[Any]
) -> None:
    """Three writes with a prune on the second: the shipped store reconnects once, a held one never."""
    out = _run(
        *("--mode", mode, "--calls", "3", "--no-reads"),
        *("--interval", "2", "--report-every", "2"),
    )

    assert len(sessions) == connects
    assert all((session is not None) == capped for session in sessions)
    assert "call 2 rss_mb=" in out
    assert "write ms on the call after each prune" in out


def test_each_read_opens_its_own_connection(sessions: list[Any]) -> None:
    """Two calls, no prune: one writer connection, then two per call for the reads."""
    out = _run("--mode", "capped", "--calls", "2")

    assert len(sessions) == 1 + 2 * 2
    assert all(session is not None for session in sessions)
    assert "read (2 queries) ms" in out


def test_an_unknown_mode_is_refused() -> None:
    """A mistyped mode must not run as another one and print figures under the typo."""
    result = runner.invoke(_mod.app, ["--mode", "cap", "--calls", "1"])

    assert result.exit_code == 2

"""Shared test fixtures.

The CLI resolves all on-disk state (DB, vector store, logs) under ``$LODE_HOME``
(default ``~/.lode``, lode-qd9). An autouse fixture pins ``$LODE_HOME`` to a
throwaway per-test directory so a test invocation never reads or writes the real
``~/.lode`` — in particular the log file handler the group callback attaches lands
in tmp, not the developer's home. Tests that exercise the default-path behaviour
explicitly still get an isolated, asserted-against directory.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_lode_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path_factory.mktemp("lode-home")
    monkeypatch.setenv("LODE_HOME", str(home))

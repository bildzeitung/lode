"""Tests for ``lode backfill`` -- the CLI surface of the backfill framework
(lode-gpzn.9).

The framework's own plumbing (registry dispatch, iterate/mint/repoint/
enqueue, the tombstone-exclusion override) is covered by
tests/test_backfill.py against ``lode.backfill`` directly; this file only
covers the CLI wiring itself: argument/flag threading, --list, and the
no-connector / unknown-connector error surfaces (CLI-only per the ticket --
no TUI parity).

Fake connectors registered here use names that are NOT "jira"/"confluence"
("fake-one", "fake-two", "fake-connector") -- both of those are now real
built-ins that ``lode backfill`` registers itself on every invocation
(lode-gpzn.10, lode-gpzn.11), so reusing either name for a fake would let the
real registration silently clobber (or be clobbered by) the fake.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lode.cli import app

runner = CliRunner()


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


def test_no_argument_lists_registered_connectors(tmp_path: Path):
    from lode.backfill import register_backfill

    register_backfill("fake-one", lambda *a: "ok")
    register_backfill("fake-two", lambda *a: "ok")
    result = runner.invoke(app, ["backfill", "--db", str(tmp_path / "lode.db")])
    assert result.exit_code == 0
    assert "fake-one" in result.output
    assert "fake-two" in result.output


def test_no_argument_lists_confluence_as_a_built_in_connector(tmp_path: Path):
    """lode-gpzn.11: unlike test_no_argument_lists_registered_connectors above
    (manually-registered fakes), "confluence" is now a real, always-available
    built-in -- lode.cli.backfill registers it itself on every invocation, no
    manual registration needed. This supersedes the old "no connectors
    registered" behavior from before any connector shipped (lode-gpzn.9)."""
    result = runner.invoke(app, ["backfill", "--db", str(tmp_path / "lode.db")])
    assert result.exit_code == 0
    assert "confluence" in result.output


def test_no_argument_lists_jira_as_a_built_in_connector(tmp_path: Path):
    """lode-gpzn.10 / lode-2uil: "jira" is a real, always-available built-in
    too -- lode.cli.backfill registers it itself on every invocation, the
    same explicit per-invocation pattern as "confluence" (no eager,
    import-time registration for either)."""
    result = runner.invoke(app, ["backfill", "--db", str(tmp_path / "lode.db")])
    assert result.exit_code == 0
    assert "jira" in result.output


def test_both_built_ins_survive_a_cleared_registry_across_repeated_invocations(
    tmp_path: Path,
):
    """Both built-ins register themselves per-invocation, not once at import
    time -- so a registry an autouse fixture clears before every test (as
    tests/test_jira_backfill.py's and tests/test_confluence_backfill.py's own
    fixtures do) never leaves either built-in missing on a later invocation,
    even under pytest-xdist where "which test imports the module first" is
    non-deterministic. Two invocations in the same test exercise exactly
    that repeated-registration path."""
    for _ in range(2):
        result = runner.invoke(app, ["backfill", "--db", str(tmp_path / "lode.db")])
        assert result.exit_code == 0
        assert "jira" in result.output
        assert "confluence" in result.output


def test_list_flag_lists_without_running_anything(tmp_path: Path):
    from lode.backfill import register_backfill

    calls = []
    register_backfill("fake-connector", lambda *a: calls.append(a) or "ok")
    result = runner.invoke(
        app, ["backfill", "fake-connector", "--list", "--db", str(tmp_path / "lode.db")]
    )
    assert result.exit_code == 0
    assert "fake-connector" in result.output
    assert calls == []  # --list short-circuits even with a connector named


def test_runs_registered_connector_and_echoes_summary(tmp_path: Path):
    from lode.backfill import register_backfill

    register_backfill(
        "fake-connector", lambda conn, settings, dry_run, retry: "migrated 3 link(s)"
    )
    result = runner.invoke(
        app, ["backfill", "fake-connector", "--db", str(tmp_path / "lode.db")]
    )
    assert result.exit_code == 0
    assert "migrated 3 link(s)" in result.output


def test_dry_run_and_retry_tombstoned_flags_thread_through(tmp_path: Path):
    from lode.backfill import register_backfill

    seen = {}

    def handler(conn, settings, dry_run, retry_tombstoned):
        seen["dry_run"] = dry_run
        seen["retry_tombstoned"] = retry_tombstoned
        return "ok"

    register_backfill("fake-connector", handler)
    result = runner.invoke(
        app,
        [
            "backfill",
            "fake-connector",
            "--dry-run",
            "--retry-tombstoned",
            "--db",
            str(tmp_path / "lode.db"),
        ],
    )
    assert result.exit_code == 0
    assert seen == {"dry_run": True, "retry_tombstoned": True}


def test_flags_default_false(tmp_path: Path):
    from lode.backfill import register_backfill

    seen = {}

    def handler(conn, settings, dry_run, retry_tombstoned):
        seen["dry_run"] = dry_run
        seen["retry_tombstoned"] = retry_tombstoned
        return "ok"

    register_backfill("fake-connector", handler)
    result = runner.invoke(
        app, ["backfill", "fake-connector", "--db", str(tmp_path / "lode.db")]
    )
    assert result.exit_code == 0
    assert seen == {"dry_run": False, "retry_tombstoned": False}


def test_unknown_connector_exits_nonzero_and_names_available(tmp_path: Path):
    from lode.backfill import register_backfill

    # "jira"/"confluence" are real built-ins as of lode-gpzn.10/.11
    # (registered by lode.cli.backfill itself on every invocation), so
    # neither is a usable stand-in for "an unregistered name" here -- use one
    # that genuinely has no handler.
    register_backfill("fake-connector", lambda *a: "ok")
    result = runner.invoke(
        app, ["backfill", "not-a-real-connector", "--db", str(tmp_path / "lode.db")]
    )
    assert result.exit_code == 1
    assert "not-a-real-connector" in result.output
    assert "fake-connector" in result.output
    # Names both real built-ins too -- proof their registration actually
    # reached the registry BackfillError reports from.
    assert "jira" in result.output
    assert "confluence" in result.output

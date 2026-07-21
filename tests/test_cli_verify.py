"""Tests for `lode verify` — read-only Atlassian connector preflight (lode-04lz).

Covers the acceptance criteria: exactly one of --jira/--confluence is
required; an inactive connector (flag off OR credentials unresolved) reports
what's missing, exits non-zero, and makes NO network call; an active
connector makes exactly one authenticated current-user GET and classifies
200/401/403/404/transient into the documented exit codes + messages; a
missing base URL with no positional-arg fallback exits non-zero with no
network call; the optional positional issue-key/URL (JIRA) or page-id/URL
(Confluence) triggers an additional read-only content dry-run; the auth
token never appears in any output. Fully offline throughout — the
connector's real HTTP fetcher (``lode.cli._default_verify_fetcher``) is
monkeypatched to return a canned, in-order stub, mirroring the
``_QueueFetcher`` pattern in tests/test_jira_fetch.py and
tests/test_confluence.py; nothing here ever imports httpx transport
internals or makes a real request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lode import cli
from lode.cli import app
from lode.webfetch import RawResponse, TransientFetchError

runner = CliRunner()

_BASE = "https://acme.atlassian.net"

# A real-shaped body.view/renderedFields payload, long enough to clear the
# default fetch_min_extract_chars floor after trafilatura extraction — mirrors
# tests/test_confluence.py's own _PAGE_JSON fixture.
_ISSUE_JSON = {
    "fields": {"summary": "Widget deploy runbook"},
    "renderedFields": {
        "description": (
            "<p>This issue documents the full deployment procedure for the "
            "widget service, including the pre-flight checklist and the "
            "rollback steps to take if the canary stage reports an "
            "elevated error rate.</p>"
            "<p>Start by confirming the on-call engineer has acknowledged "
            "the deploy window, then proceed through each stage in order, "
            "watching the dashboards closely at every step of the "
            "rollout.</p>"
        )
    },
}
_COMMENTS_EMPTY = {"startAt": 0, "maxResults": 0, "total": 0, "comments": []}
_PAGE_JSON = {
    "id": "123456",
    "body": {
        "view": {
            "value": (
                "<div><h1>Runbook</h1>"
                "<p>This page documents the full deployment procedure for "
                "the widget service, including the pre-flight checklist and "
                "the rollback steps to take if the canary stage reports an "
                "elevated error rate.</p>"
                "<p>Start by confirming the on-call engineer has "
                "acknowledged the deploy window, then proceed through each "
                "stage in order, watching the dashboards closely at every "
                "step of the rollout.</p></div>"
            )
        }
    },
}
_EMPTY_PAGE_JSON = {"id": "123456", "body": {"view": {"value": "<div></div>"}}}


class _QueueFetcher:
    """Stub Fetcher returning canned responses (or raising) in order.

    Same shape as tests/test_jira_fetch.py's ``_QueueFetcher`` — supports the
    sequential calls a single `lode verify` invocation makes: the
    current-user probe, then (when a content dry-run also runs) the
    issue/page fetch and, for JIRA, its comment-page fetch.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(
    payload: dict, *, url: str = "irrelevant", status_code: int = 200
) -> RawResponse:
    return RawResponse(final_url=url, status_code=status_code, text=json.dumps(payload))


def _no_fetcher_allowed(*_args: object, **_kwargs: object):
    """Fails the test loudly if a fetcher is ever constructed.

    Injected in place of ``lode.cli._default_verify_fetcher`` for the
    "inactive" / "missing base URL" cases, where the acceptance criteria
    require NO network call is ever attempted.
    """
    raise AssertionError(
        "a fetcher was constructed -- a network call was attempted when none "
        "should have been made"
    )


def _write_config(home: Path, **kv: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = []
    for key, value in kv.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        else:
            lines.append(f'{key} = "{value}"')
    (home / "config.toml").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_verify_requires_exactly_one_flag_zero_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code != 0
    assert "exactly one" in result.stdout


def test_verify_requires_exactly_one_flag_both_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    result = runner.invoke(app, ["verify", "--jira", "--confluence"])
    assert result.exit_code != 0
    assert "exactly one" in result.stdout


# ---------------------------------------------------------------------------
# Inactive connector -- flag off, or creds unresolved -- no network call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--jira", "--confluence"])
def test_verify_inactive_flag_off_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    home = tmp_path / "home"
    result = runner.invoke(app, ["verify", flag], env={"LODE_HOME": str(home)})
    assert result.exit_code != 0
    assert "inactive" in result.stdout
    assert "enabled is False" in result.stdout


@pytest.mark.parametrize(
    ("flag", "enabled_key"),
    [("--jira", "jira_enabled"), ("--confluence", "confluence_enabled")],
)
def test_verify_inactive_creds_unresolved_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, enabled_key: str
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    home = tmp_path / "home"
    _write_config(home, **{enabled_key: True})
    result = runner.invoke(app, ["verify", flag], env={"LODE_HOME": str(home)})
    assert result.exit_code != 0
    assert "inactive" in result.stdout
    assert "credentials are unresolved" in result.stdout


# ---------------------------------------------------------------------------
# Missing base URL, no positional arg -- non-zero, no network call
# ---------------------------------------------------------------------------


def test_verify_missing_base_url_and_no_arg_exits_nonzero_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    home = tmp_path / "home"
    _write_config(home, jira_enabled=True)
    result = runner.invoke(
        app,
        ["verify", "--jira"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "super-secret-token",
        },
    )
    assert result.exit_code != 0
    assert "no base URL available" in result.stdout
    assert "base_url" in result.stdout or "positional argument" in result.stdout


# ---------------------------------------------------------------------------
# Active + 200 -- exit 0, display name printed, token never in output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "base_key", "email_env", "token_env"),
    [
        ("--jira", "jira_base_url", "LODE_JIRA_EMAIL", "LODE_JIRA_TOKEN"),
        (
            "--confluence",
            "confluence_base_url",
            "LODE_CONFLUENCE_EMAIL",
            "LODE_CONFLUENCE_TOKEN",
        ),
    ],
)
def test_verify_active_200_exits_zero_and_never_echoes_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    base_key: str,
    email_env: str,
    token_env: str,
) -> None:
    enabled_key = "jira_enabled" if flag == "--jira" else "confluence_enabled"
    home = tmp_path / "home"
    _write_config(home, **{enabled_key: True, base_key: _BASE})

    secret_token = "super-secret-token-xyz"
    fetcher = _QueueFetcher([_response({"displayName": "Alice Example"})])
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", flag],
        env={
            "LODE_HOME": str(home),
            email_env: "alice@example.com",
            token_env: secret_token,
        },
    )
    assert result.exit_code == 0
    assert "Alice Example" in result.stdout
    assert secret_token not in result.stdout
    assert len(fetcher.calls) == 1


def test_verify_jira_probes_the_documented_myself_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_config(home, jira_enabled=True, jira_base_url=_BASE)
    fetcher = _QueueFetcher([_response({"displayName": "Alice"})])
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--jira"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert fetcher.calls == [f"{_BASE}/rest/api/3/myself"]


def test_verify_confluence_probes_the_documented_current_user_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_config(home, confluence_enabled=True, confluence_base_url=_BASE)
    fetcher = _QueueFetcher([_response({"displayName": "Bob"})])
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--confluence"],
        env={
            "LODE_HOME": str(home),
            "LODE_CONFLUENCE_EMAIL": "bob@example.com",
            "LODE_CONFLUENCE_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert fetcher.calls == [f"{_BASE}/wiki/rest/api/user/current"]


# ---------------------------------------------------------------------------
# Active + 401 -- non-zero, credentials message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--jira", "--confluence"])
def test_verify_active_401_reports_credentials_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    enabled_key = "jira_enabled" if flag == "--jira" else "confluence_enabled"
    base_key = "jira_base_url" if flag == "--jira" else "confluence_base_url"
    email_env = "LODE_JIRA_EMAIL" if flag == "--jira" else "LODE_CONFLUENCE_EMAIL"
    token_env = "LODE_JIRA_TOKEN" if flag == "--jira" else "LODE_CONFLUENCE_TOKEN"
    home = tmp_path / "home"
    _write_config(home, **{enabled_key: True, base_key: _BASE})

    fetcher = _QueueFetcher([_response({}, status_code=401)])
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", flag],
        env={"LODE_HOME": str(home), email_env: "a@b.com", token_env: "wrong-token"},
    )
    assert result.exit_code != 0
    assert "credentials" in result.stdout
    assert "wrong-token" not in result.stdout


# ---------------------------------------------------------------------------
# Active + 404 -- non-zero, base URL message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--jira", "--confluence"])
def test_verify_active_404_reports_base_url_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    enabled_key = "jira_enabled" if flag == "--jira" else "confluence_enabled"
    base_key = "jira_base_url" if flag == "--jira" else "confluence_base_url"
    email_env = "LODE_JIRA_EMAIL" if flag == "--jira" else "LODE_CONFLUENCE_EMAIL"
    token_env = "LODE_JIRA_TOKEN" if flag == "--jira" else "LODE_CONFLUENCE_TOKEN"
    home = tmp_path / "home"
    _write_config(home, **{enabled_key: True, base_key: _BASE})

    fetcher = _QueueFetcher([_response({}, status_code=404)])
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", flag],
        env={"LODE_HOME": str(home), email_env: "a@b.com", token_env: "tok"},
    )
    assert result.exit_code != 0
    assert "base_url" in result.stdout or "base URL" in result.stdout


# ---------------------------------------------------------------------------
# Active + transient -- non-zero, unreachable message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--jira", "--confluence"])
def test_verify_active_transient_reports_unreachable_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    enabled_key = "jira_enabled" if flag == "--jira" else "confluence_enabled"
    base_key = "jira_base_url" if flag == "--jira" else "confluence_base_url"
    email_env = "LODE_JIRA_EMAIL" if flag == "--jira" else "LODE_CONFLUENCE_EMAIL"
    token_env = "LODE_JIRA_TOKEN" if flag == "--jira" else "LODE_CONFLUENCE_TOKEN"
    home = tmp_path / "home"
    _write_config(home, **{enabled_key: True, base_key: _BASE})

    fetcher = _QueueFetcher([TransientFetchError("timeout: connect timed out")])
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", flag],
        env={"LODE_HOME": str(home), email_env: "a@b.com", token_env: "tok"},
    )
    assert result.exit_code != 0
    assert "unreachable" in result.stdout


# ---------------------------------------------------------------------------
# Optional positional issue-key/URL (JIRA) or page-id/URL (Confluence) --
# additional read-only content dry-run
# ---------------------------------------------------------------------------


def test_verify_jira_optional_issue_arg_content_dry_run_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_config(home, jira_enabled=True, jira_base_url=_BASE)
    fetcher = _QueueFetcher(
        [
            _response({"displayName": "Alice"}),
            _response(_ISSUE_JSON),
            _response(_COMMENTS_EMPTY),
        ]
    )
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--jira", "ABC-123"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert "content dry-run (ABC-123): OK" in result.stdout


def test_verify_jira_optional_issue_arg_content_dry_run_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_config(home, jira_enabled=True, jira_base_url=_BASE)
    fetcher = _QueueFetcher(
        [
            _response({"displayName": "Alice"}),
            _response({}, status_code=404),
        ]
    )
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--jira", "ABC-999"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "tok",
        },
    )
    # Auth succeeded (exit 0 is defined purely by the current-user probe) --
    # the content dry-run's tombstone is reported, not fatal.
    assert result.exit_code == 0
    assert "content dry-run (ABC-999): tombstoned (http_404)" in result.stdout


def test_verify_jira_content_dry_run_transient_does_not_affect_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient blip on the content dry-run, after the auth probe already
    succeeded, is reported and shrugged off -- it never changes the exit code
    (only the auth probe does) and never crashes with a traceback."""
    home = tmp_path / "home"
    _write_config(home, jira_enabled=True, jira_base_url=_BASE)
    fetcher = _QueueFetcher(
        [
            _response({"displayName": "Alice"}),
            TransientFetchError("timeout: read timed out"),
        ]
    )
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--jira", "ABC-123"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert "content dry-run (ABC-123): tenant unreachable" in result.stdout


def test_verify_confluence_optional_page_arg_content_dry_run_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_config(home, confluence_enabled=True, confluence_base_url=_BASE)
    fetcher = _QueueFetcher(
        [
            _response({"displayName": "Bob"}),
            _response(_PAGE_JSON),
        ]
    )
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--confluence", "123456"],
        env={
            "LODE_HOME": str(home),
            "LODE_CONFLUENCE_EMAIL": "bob@example.com",
            "LODE_CONFLUENCE_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert "content dry-run (123456): OK" in result.stdout


def test_verify_confluence_optional_page_arg_content_dry_run_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_config(home, confluence_enabled=True, confluence_base_url=_BASE)
    fetcher = _QueueFetcher(
        [
            _response({"displayName": "Bob"}),
            _response(_EMPTY_PAGE_JSON),
        ]
    )
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--confluence", "123456"],
        env={
            "LODE_HOME": str(home),
            "LODE_CONFLUENCE_EMAIL": "bob@example.com",
            "LODE_CONFLUENCE_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert "content dry-run (123456): tombstoned (empty_extract)" in result.stdout


def test_verify_optional_arg_as_full_url_derives_base_and_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positional arg doubles as the base-URL source when {connector}_base_url
    is unset (Base URL handling, docs/externals.md ~L437-518 smoke test)."""
    home = tmp_path / "home"
    _write_config(home, jira_enabled=True)  # no jira_base_url configured
    fetcher = _QueueFetcher(
        [
            _response({"displayName": "Alice"}),
            _response(_ISSUE_JSON),
            _response(_COMMENTS_EMPTY),
        ]
    )
    monkeypatch.setattr(cli, "_default_verify_fetcher", lambda *a, **k: fetcher)

    result = runner.invoke(
        app,
        ["verify", "--jira", f"{_BASE}/browse/ABC-123"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "tok",
        },
    )
    assert result.exit_code == 0
    assert fetcher.calls[0] == f"{_BASE}/rest/api/3/myself"
    assert fetcher.calls[1] == f"{_BASE}/rest/api/3/issue/ABC-123?expand=renderedFields"


# ---------------------------------------------------------------------------
# Credential source reporting -- env var vs config.toml, token always redacted
# ---------------------------------------------------------------------------


def test_verify_reports_credential_source_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    home = tmp_path / "home"
    result = runner.invoke(
        app,
        ["verify", "--jira"],
        env={
            "LODE_HOME": str(home),
            "LODE_JIRA_EMAIL": "alice@example.com",
            "LODE_JIRA_TOKEN": "super-secret-token",
        },
    )
    # jira_enabled is still False -- inactive, but credentials themselves
    # resolve, and their SOURCE is reported without ever echoing the token.
    assert "env var LODE_JIRA_EMAIL" in result.stdout
    assert "env var LODE_JIRA_TOKEN" in result.stdout
    assert "super-secret-token" not in result.stdout


def test_verify_reports_credential_source_config_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_default_verify_fetcher", _no_fetcher_allowed)
    home = tmp_path / "home"
    _write_config(
        home,
        jira_email="alice@example.com",
        jira_token="super-secret-token",
    )
    result = runner.invoke(app, ["verify", "--jira"], env={"LODE_HOME": str(home)})
    assert "config.toml" in result.stdout
    assert "super-secret-token" not in result.stdout

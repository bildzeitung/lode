"""Tests for lode.auth — Anthropic credential resolution (lode-txh.4, lode-tcq).

Asserts the acceptance criteria: credentials resolve from ``ANTHROPIC_API_KEY``
OR an ``ant auth login`` profile OR the Claude Code login OAuth token, with no
hardcoded key, and a fully missing credential produces a clear, actionable error.

Determinism: tests that exercise the *missing* / OAuth paths point
``ANTHROPIC_CONFIG_DIR`` at an empty temp dir, clear the credential env vars, and
redirect ``auth.CLAUDE_CODE_CREDENTIALS`` at a temp path, so neither a real ``ant
auth login`` profile nor a real Claude Code login on the dev machine can leak in.
Branch-level cases monkeypatch ``anthropic.Anthropic`` so each resolution outcome
is exercised in isolation without a network call.
"""

import json
import time
from pathlib import Path

import anthropic
import pytest

from lode import auth
from lode.auth import AuthError, build_client

CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")


@pytest.fixture
def isolated_sdk(monkeypatch, tmp_path):
    """Make the SDK chain resolve nothing: no env vars, empty config dir."""
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "empty-config"))


@pytest.fixture
def no_ambient_credentials(monkeypatch, tmp_path, isolated_sdk):
    """SDK resolves nothing AND no Claude Code login exists on disk."""
    monkeypatch.setattr(
        auth, "CLAUDE_CODE_CREDENTIALS", tmp_path / "no-claude" / ".credentials.json"
    )


def _write_claude_login(path: Path, *, token: str, expires_at_ms: int | None) -> None:
    """Write a Claude-Code-shaped ``.credentials.json`` to ``path``."""
    oauth: dict[str, object] = {"accessToken": token}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


def test_no_hardcoded_key_in_source() -> None:
    # No literal key is embedded: build_client constructs Anthropic() with no
    # api_key argument and resolution is delegated to the SDK / Claude Code login.
    src = Path(auth.__file__).read_text()
    assert "sk-ant" not in src
    assert "api_key=" not in src


def test_missing_credentials_raises_actionable_error(no_ambient_credentials) -> None:
    with pytest.raises(AuthError) as excinfo:
        build_client()
    message = str(excinfo.value)
    # The error names every supported resolution path.
    assert "ANTHROPIC_API_KEY" in message
    assert "ant auth login" in message
    assert "Claude Code" in message


def test_env_api_key_resolves(monkeypatch, tmp_path, isolated_sdk) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-env-key")

    client = build_client()  # real SDK, no network call

    assert isinstance(client, anthropic.Anthropic)
    assert client.api_key == "sk-ant-test-env-key"


def test_profile_provider_not_falsely_rejected(monkeypatch) -> None:
    # An `ant auth login` profile installs a credentials provider (not an
    # api_key/auth_token). build_client must accept that, not reject it.
    class _ProfileClient:
        api_key = None
        auth_token = None
        credentials = object()  # a resolved provider stands in for a profile

    monkeypatch.setattr(anthropic, "Anthropic", lambda: _ProfileClient())

    assert isinstance(build_client(), _ProfileClient)


def test_oauth_login_resolves_when_sdk_finds_nothing(
    monkeypatch, tmp_path, isolated_sdk
) -> None:
    # SDK resolves nothing, but a Claude Code login is present: build_client falls
    # back to it, sending the OAuth token as a Bearer auth_token (real SDK, no
    # network call). One hour of validity left.
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_claude_login(
        creds, token="sk-ant-oat-test", expires_at_ms=int((time.time() + 3600) * 1000)
    )
    monkeypatch.setattr(auth, "CLAUDE_CODE_CREDENTIALS", creds)

    client = build_client()

    assert isinstance(client, anthropic.Anthropic)
    assert client.auth_token == "sk-ant-oat-test"
    assert client.api_key is None


def test_oauth_login_without_expiry_is_accepted(
    monkeypatch, tmp_path, isolated_sdk
) -> None:
    # A login file lacking expiresAt is treated as usable (no expiry to enforce).
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_claude_login(creds, token="sk-ant-oat-noexp", expires_at_ms=None)
    monkeypatch.setattr(auth, "CLAUDE_CODE_CREDENTIALS", creds)

    assert build_client().auth_token == "sk-ant-oat-noexp"


def test_expired_oauth_token_is_rejected(monkeypatch, tmp_path, isolated_sdk) -> None:
    # An expired Claude Code token is not used; with nothing else to resolve,
    # build_client raises the actionable AuthError rather than sending a dead token.
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_claude_login(
        creds, token="sk-ant-oat-old", expires_at_ms=int((time.time() - 60) * 1000)
    )
    monkeypatch.setattr(auth, "CLAUDE_CODE_CREDENTIALS", creds)

    with pytest.raises(AuthError):
        build_client()


def test_malformed_oauth_file_is_rejected(monkeypatch, tmp_path, isolated_sdk) -> None:
    # A corrupt login file falls through to the actionable AuthError, not a crash.
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(auth, "CLAUDE_CODE_CREDENTIALS", creds)

    with pytest.raises(AuthError):
        build_client()


def test_construction_error_maps_to_auth_error(
    monkeypatch, no_ambient_credentials
) -> None:
    # When the SDK raises at construction (e.g. a selected profile file is missing)
    # and no Claude Code login is available, it is wrapped in the actionable
    # AuthError, chained for debugging.
    original = anthropic.AnthropicError("Config file not found (profile 'default').")

    def _raise() -> None:
        raise original

    monkeypatch.setattr(anthropic, "Anthropic", _raise)

    with pytest.raises(AuthError) as excinfo:
        build_client()
    assert excinfo.value.__cause__ is original
    assert "ant auth login" in str(excinfo.value)

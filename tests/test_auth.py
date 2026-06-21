"""Tests for lode.auth — Anthropic credential resolution (lode-txh.4).

Asserts the acceptance criteria: the key resolves from ``ANTHROPIC_API_KEY`` OR
an ``ant auth login`` profile with no hardcoded key, and a missing key produces
a clear, actionable error.

Determinism: tests that exercise the *missing* path point ``ANTHROPIC_CONFIG_DIR``
at an empty temp dir and clear the credential env vars, so a real ``ant auth
login`` profile on the dev machine can't leak in. Branch-level cases monkeypatch
``anthropic.Anthropic`` so each resolution outcome is exercised in isolation
without a network call.
"""

from pathlib import Path

import anthropic
import pytest

from lode import auth
from lode.auth import AuthError, build_client

CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")


@pytest.fixture
def no_ambient_credentials(monkeypatch, tmp_path):
    """Isolate from any real env var or on-disk ``ant auth login`` profile."""
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "empty-config"))


def test_no_hardcoded_key_in_source() -> None:
    # No literal key is embedded: build_client constructs Anthropic() with no
    # api_key argument and resolution is delegated to the SDK.
    src = Path(auth.__file__).read_text()
    assert "sk-ant" not in src
    assert "api_key=" not in src


def test_missing_credentials_raises_actionable_error(no_ambient_credentials) -> None:
    with pytest.raises(AuthError) as excinfo:
        build_client()
    message = str(excinfo.value)
    # The error names both supported resolution paths.
    assert "ANTHROPIC_API_KEY" in message
    assert "ant auth login" in message


def test_env_api_key_resolves(monkeypatch, tmp_path) -> None:
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "empty-config"))
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


def test_construction_error_maps_to_auth_error(monkeypatch) -> None:
    # When the SDK raises at construction (e.g. a selected profile file is
    # missing), it is wrapped in the actionable AuthError, chained for debugging.
    original = anthropic.AnthropicError("Config file not found (profile 'default').")

    def _raise() -> None:
        raise original

    monkeypatch.setattr(anthropic, "Anthropic", _raise)

    with pytest.raises(AuthError) as excinfo:
        build_client()
    assert excinfo.value.__cause__ is original
    assert "ant auth login" in str(excinfo.value)

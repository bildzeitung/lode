"""Tests for lode.auth — Anthropic credential resolution (lode-txh.4).

Asserts the acceptance criteria: credentials resolve from ``ANTHROPIC_API_KEY``
OR an ``ant auth login`` profile, with no hardcoded key, and a fully missing
credential produces a clear, actionable error.

Determinism: tests that exercise the *missing* credential path point
``ANTHROPIC_CONFIG_DIR`` at an empty temp dir and clear the credential env vars,
so a real ``ant auth login`` profile on the dev machine can't leak in.
Branch-level cases monkeypatch ``anthropic.Anthropic`` so each resolution outcome
is exercised in isolation without a network call.

Two cases below (``test_missing_credentials_raises_actionable_error``,
``test_env_api_key_resolves``) deliberately construct the *real*, un-mocked
``anthropic.Anthropic()`` — that's the whole point, exercising the SDK's own
credential-chain behavior that :func:`lode.auth.build_client` wraps. Neither
makes a network call (construction alone never does), so they carry
``@pytest.mark.network`` purely to lift ``tests/conftest.py``'s autouse
LLM-client-construction guard (lode-85q), which otherwise can't distinguish
"real construction on purpose" from "real construction because a mock broke".
"""

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


def test_no_hardcoded_key_in_source() -> None:
    # No literal key is embedded: build_client constructs Anthropic() with no
    # api_key argument and resolution is delegated entirely to the SDK.
    src = Path(auth.__file__).read_text()
    assert "sk-ant" not in src
    assert "api_key=" not in src


@pytest.mark.network
def test_missing_credentials_raises_actionable_error(isolated_sdk) -> None:
    with pytest.raises(AuthError) as excinfo:
        build_client()
    message = str(excinfo.value)
    # The error names every supported resolution path.
    assert "ANTHROPIC_API_KEY" in message
    assert "ant auth login" in message


@pytest.mark.network
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


def test_construction_error_maps_to_auth_error(monkeypatch, isolated_sdk) -> None:
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

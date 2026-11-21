"""Tests for lode.auth — Anthropic credential resolution (lode-txh.4).

Asserts the acceptance criteria: credentials resolve from ``ANTHROPIC_API_KEY``
OR an ``ant auth login`` profile, with no hardcoded key, and a fully missing
credential produces a clear, actionable error.

Determinism: tests that exercise the *missing* credential path point
``ANTHROPIC_CONFIG_DIR`` at an empty temp dir and clear the credential env vars,
so a real ``ant auth login`` profile on the dev machine can't leak in.
Branch-level cases monkeypatch ``anthropic.Anthropic`` so each resolution outcome
is exercised in isolation without a network call.

Some cases below (``test_constructed_client_has_tracing_off``,
``test_missing_credentials_raises_actionable_error``,
``test_env_api_key_resolves``) deliberately construct the *real*, un-mocked
``anthropic.Anthropic()`` — that's the whole point, exercising the SDK's own
behavior that :func:`lode.auth.build_client` wraps. None makes a network call
(construction alone never does), so they carry
``@pytest.mark.network`` purely to lift ``tests/conftest.py``'s autouse
LLM-client-construction guard (lode-85q), which otherwise can't distinguish
"real construction on purpose" from "real construction because a mock broke".
"""

import subprocess
import sys
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
def test_constructed_client_has_tracing_off(monkeypatch, isolated_sdk) -> None:
    """The client ``build_client`` constructs has the SDK's OpenTelemetry tracing off, even
    when the environment asks for it (lode-sflg)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-env-key")
    # The SDK traces unless this variable is "false", so "true" keeps an ambient "false" on
    # the machine running the suite from passing this test without the argument.
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY", "true")

    client = build_client()  # real SDK, no network call

    assert client.open_telemetry["traces"]["enabled"] is False


@pytest.mark.parametrize("module", ["lode.auth", "lode.enrich", "lode.tui.screens.ask"])
def test_importing_module_does_not_import_the_sdk(module: str) -> None:
    """Importing ``lode.auth`` / ``lode.enrich`` / the TUI's ask screen must NOT
    import ``anthropic`` (lode-4q97).

    This is the **boundary** guard, and it is deliberately stated about the modules
    rather than about any one caller. Each is imported on paths that never touch
    Anthropic and may have no credentials at all:

    * ``lode.auth`` -- :mod:`lode.worker` imports ``AuthError`` from it at module
      scope (its ``except`` headers need the class), so every drain loads it;
    * ``lode.enrich`` -- :mod:`lode.reconcile` imports ``ENRICH_PROMPT_VER`` from it
      at module level, and ``lode work`` runs a reconcile pass on every invocation;
      and
    * ``lode.tui.screens.ask`` -- :mod:`lode.tui.app` imports it to register the
      screen, so every TUI start loads it.

    So a credential-free, embed-only drain (embeds come from the LOCAL fastembed
    model) would otherwise pay the ~0.7s SDK import on every single run to do
    nothing with it. ``lode.auth`` has no module-level ``import anthropic`` (it is
    deferred inside ``build_client``), and neither does ``lode.enrich`` (its
    provider import is deferred inside ``lode.llm_provider``).

    Asserting this at the module boundary -- rather than once per known call site --
    is what makes it hold for *future* callers too: a new eager
    ``from lode.enrich import ...`` anywhere in the codebase stays free, and cannot
    silently reintroduce the cost.

    Runs in a **subprocess** deliberately. A fresh interpreter is the real-world
    condition this is about, and it is the only way to answer the question honestly:
    in-process, every one of these modules is already resident (this very file
    imports two of them), so the assertion would be vacuous -- and evicting them
    from ``sys.modules`` to un-vacuum it re-imports them, which rebinds the
    submodule as an attribute of the ``lode`` package and leaves a duplicate module
    object behind to poison later tests. A subprocess sidesteps both traps.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}, sys; print('anthropic' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert proc.stdout.strip() == "False", (
        f"importing {module} pulled in the Anthropic SDK -- keep `import anthropic` "
        "TYPE_CHECKING-guarded (or inside the function that builds a client)"
    )


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

    monkeypatch.setattr(anthropic, "Anthropic", lambda **_kw: _ProfileClient())

    assert isinstance(build_client(), _ProfileClient)


def test_construction_error_maps_to_auth_error(monkeypatch, isolated_sdk) -> None:
    # When the SDK raises at construction (e.g. a selected profile file is
    # missing), it is wrapped in the actionable AuthError, chained for debugging.
    original = anthropic.AnthropicError("Config file not found (profile 'default').")

    def _raise(**_kw: object) -> None:
        raise original

    monkeypatch.setattr(anthropic, "Anthropic", _raise)

    with pytest.raises(AuthError) as excinfo:
        build_client()
    assert excinfo.value.__cause__ is original
    assert "ant auth login" in str(excinfo.value)

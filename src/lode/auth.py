"""Anthropic credential resolution for lode (lode-txh.4).

lode never embeds an API key (``docs/stack.md``): credentials are resolved the
same way the sibling harness resolves them -- by the Anthropic SDK itself. The
SDK checks, in order, ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``, then an
``ANTHROPIC_PROFILE`` / ``ant auth login`` profile on disk, then workload-identity
federation.

:func:`build_client` delegates that resolution to the SDK (passing no key) and
turns the "nothing resolved" outcomes into one clear, actionable error -- so a
missing credential fails loudly at startup with guidance, instead of as an opaque
error at the first API call.
"""

from __future__ import annotations

import anthropic

#: Actionable message naming both supported resolution paths. No key is ever
#: embedded in lode, so the only fixes are an env var or an ``ant auth login``
#: profile.
MISSING_CREDENTIALS_MESSAGE = (
    "No Anthropic credentials found. Set the ANTHROPIC_API_KEY environment "
    "variable, or run `ant auth login` to sign in with a profile "
    "(see docs/stack.md). lode never embeds an API key."
)


class AuthError(RuntimeError):
    """No Anthropic credentials could be resolved -- raised by :func:`build_client`."""


def build_client() -> anthropic.Anthropic:
    """Return an Anthropic client with credentials resolved by the SDK.

    No API key is passed in -- resolution (env var, then ``ant auth login``
    profile, then workload-identity federation) is the SDK's job. When the SDK
    resolves nothing, raise :class:`AuthError` with an actionable message.

    The SDK signals "no credentials" two ways depending on configuration: it
    raises ``anthropic.AnthropicError`` at construction (e.g. a selected profile
    file is missing), or it constructs a client with no key, token, or provider
    at all. Both map to the same :class:`AuthError`.
    """
    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as err:
        raise AuthError(MISSING_CREDENTIALS_MESSAGE) from err
    if not _has_credentials(client):
        raise AuthError(MISSING_CREDENTIALS_MESSAGE)
    return client


def _has_credentials(client: anthropic.Anthropic) -> bool:
    """True when the SDK resolved any credential (static key/token or provider).

    A static key/token sets ``api_key`` / ``auth_token``; a profile or
    federation config installs a ``credentials`` provider. All three being unset
    is the only "no credentials" state. ``credentials`` is read defensively so a
    differing SDK version that lacks the attribute still works via key/token.
    """
    return (
        client.api_key is not None
        or client.auth_token is not None
        or getattr(client, "credentials", None) is not None
    )

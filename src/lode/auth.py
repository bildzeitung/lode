"""Anthropic credential resolution for lode (lode-txh.4, lode-tcq).

lode never embeds an API key (``docs/stack.md``): credentials are resolved by the
Anthropic SDK itself, with one local fallback. The order is:

1. **The SDK chain** -- ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``, then an
   ``ANTHROPIC_PROFILE`` / ``ant auth login`` profile on disk, then
   workload-identity federation.
2. **The Claude Code login** -- when the SDK resolves nothing, fall back to the
   OAuth token the Claude Code CLI writes to ``~/.claude/.credentials.json``
   (``claudeAiOauth.accessToken``), sent as a Bearer ``auth_token`` with the
   OAuth beta header. This lets lode ride an existing Claude Code session on a
   machine that has no API key or ``ant`` profile set up.

:func:`build_client` runs that order and turns the "nothing resolved" outcome
into one clear, actionable :class:`AuthError` -- so a missing credential fails
loudly with guidance (the caller renders it without a traceback and logs the
details) instead of as an opaque error at the first API call.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import anthropic

_log = logging.getLogger(__name__)

#: Where the Claude Code CLI persists its login. lode reads (never writes) this
#: file for the OAuth fallback; the token is refreshed by Claude Code itself.
CLAUDE_CODE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

#: Beta header the API requires for Claude Code OAuth (Bearer) tokens.
_OAUTH_BETA_HEADER = "oauth-2025-04-20"

#: Actionable message naming every resolution path. No key is ever embedded in
#: lode, so the only fixes are an env var, an ``ant auth login`` profile, or a
#: Claude Code login.
MISSING_CREDENTIALS_MESSAGE = (
    "No Anthropic credentials found. lode resolves them in this order: the "
    "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) environment variable, an "
    "`ant auth login` profile, then your Claude Code login "
    f"({CLAUDE_CODE_CREDENTIALS}). Set one of these (see docs/stack.md) -- "
    "e.g. run `claude` to sign in. lode never embeds an API key."
)


class AuthError(RuntimeError):
    """No Anthropic credentials could be resolved -- raised by :func:`build_client`."""


def build_client() -> anthropic.Anthropic:
    """Return an Anthropic client with resolved credentials, or raise :class:`AuthError`.

    Tries the SDK credential chain first (env var, then ``ant auth login``
    profile, then workload-identity federation -- all the SDK's job, no key is
    passed in). When that resolves nothing, falls back to the Claude Code login
    (:func:`_oauth_client`). When neither resolves a credential, raises
    :class:`AuthError` with an actionable message.

    The SDK signals "no credentials" two ways: it raises
    ``anthropic.AnthropicError`` at construction (e.g. a selected profile file is
    missing), or it constructs a client with no key, token, or provider at all.
    Both fall through to the OAuth fallback; if that also fails, the construction
    error (when any) is chained onto the :class:`AuthError` for debugging.
    """
    sdk_error: anthropic.AnthropicError | None = None
    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as err:
        sdk_error = err
        _log.debug("SDK could not construct a credentialed client: %s", err)
    else:
        if _has_credentials(client):
            return client

    oauth_client = _oauth_client()
    if oauth_client is not None:
        return oauth_client

    raise AuthError(MISSING_CREDENTIALS_MESSAGE) from sdk_error


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


def _oauth_client() -> anthropic.Anthropic | None:
    """Build a client from the Claude Code login, or ``None`` if unavailable.

    Reads the OAuth access token Claude Code writes to
    :data:`CLAUDE_CODE_CREDENTIALS` and returns a client that sends it as a
    Bearer ``auth_token`` with the required OAuth beta header. Returns ``None``
    (logging why) when the file is absent, malformed, or holds an expired token
    -- the caller turns that into the actionable :class:`AuthError`. lode only
    reads this file; Claude Code owns refreshing the token.
    """
    try:
        raw = CLAUDE_CODE_CREDENTIALS.read_text(encoding="utf-8")
    except OSError as err:
        _log.debug("No Claude Code login at %s: %s", CLAUDE_CODE_CREDENTIALS, err)
        return None
    try:
        oauth = json.loads(raw)["claudeAiOauth"]
        token = oauth["accessToken"]
        expires_at = oauth.get("expiresAt")
    except (ValueError, KeyError, TypeError) as err:
        _log.warning(
            "Claude Code login at %s is malformed: %s", CLAUDE_CODE_CREDENTIALS, err
        )
        return None
    if expires_at is not None and expires_at / 1000 <= time.time():
        _log.warning(
            "Claude Code OAuth token at %s has expired; run `claude` to refresh it.",
            CLAUDE_CODE_CREDENTIALS,
        )
        return None
    _log.info(
        "Resolved Anthropic credentials from the Claude Code login (%s).",
        CLAUDE_CODE_CREDENTIALS,
    )
    return anthropic.Anthropic(
        auth_token=token,
        default_headers={"anthropic-beta": _OAUTH_BETA_HEADER},
    )

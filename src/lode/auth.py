"""Anthropic credential resolution for lode (lode-txh.4).

lode never embeds an API key (``docs/stack.md``): credentials are resolved by the
Anthropic SDK's own credential chain -- ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``,
then an ``ANTHROPIC_PROFILE`` / ``ant auth login`` profile on disk, then workload-identity
federation.

:func:`build_client` runs that chain and turns the "nothing resolved" outcome into
one clear, actionable :class:`AuthError` -- so a missing credential fails loudly
with guidance (the caller renders it without a traceback and logs the details)
instead of as an opaque error at the first API call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only — `from __future__ import annotations` keeps
    import anthropic  # them strings at runtime, so this costs nothing.

_log = logging.getLogger(__name__)

#: Actionable message naming every resolution path. No key is ever embedded in
#: lode, so the only fixes are an env var or an ``ant auth login`` profile.
MISSING_CREDENTIALS_MESSAGE = (
    "No Anthropic credentials found. lode resolves them in this order: the "
    "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) environment variable, then an "
    "`ant auth login` profile. Set one of these (see docs/stack.md). lode never "
    "embeds an API key."
)


class AuthError(RuntimeError):
    """No Anthropic credentials could be resolved -- raised by :func:`build_client`.

    Deliberately a plain :class:`RuntimeError` subclass with no SDK dependency, so
    that **importing this module costs nothing** (lode-4q97). Callers that only
    need to *catch* the error -- ``worker.drain``, ``worker.run_one``, ``cli``,
    the TUI ask screen -- do ``from lode.auth import AuthError`` on paths that may
    never touch Anthropic at all (most importantly a credential-free, embed-only
    drain, whose embeds come from the LOCAL fastembed model). That import must not
    drag in the ~0.32s Anthropic SDK import, so ``import anthropic`` lives inside
    :func:`build_client` rather than at module level.
    """


def build_client() -> anthropic.Anthropic:
    """Return an Anthropic client with resolved credentials, or raise :class:`AuthError`.

    Tries the SDK credential chain (env var, then ``ant auth login`` profile,
    then workload-identity federation -- all the SDK's job, no key is passed
    in). When that resolves nothing, raises :class:`AuthError` with an
    actionable message.

    The SDK signals "no credentials" two ways: it raises
    ``anthropic.AnthropicError`` at construction (e.g. a selected profile file is
    missing), or it constructs a client with no key, token, or provider at all.
    Both map to :class:`AuthError`; the construction error (when any) is chained
    onto it for debugging.
    """
    # Deferred so that merely importing `lode.auth` (e.g. for AuthError alone) does
    # not pay for the SDK -- see the note on AuthError above (lode-4q97). Actually
    # *building* a client obviously needs it, so it is never a waste here.
    import anthropic

    sdk_error: anthropic.AnthropicError | None = None
    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as err:
        sdk_error = err
        _log.debug("SDK could not construct a credentialed client: %s", err)
    else:
        if _has_credentials(client):
            return client

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

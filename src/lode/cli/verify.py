"""``lode verify`` -- read-only preflight for one Atlassian connector (lode-04lz).

Confirms lode's OWN resolution (jira_active/confluence_active) flips true,
then makes ONE authenticated, read-only GET at the connector's current-user
endpoint to prove the resolved credentials actually reach the tenant -- no DB
rows, no job enqueue, no embedding, no vector-store writes. Reuses every
existing seam (lode.config's credential resolvers/active() checks/
AtlassianCredentials, the JIRA/Confluence HttpxFetcher subclasses, the shared
HTTP-status classifier, the pure fetch units, drawdown._resolve_api_base)
rather than building parallel machinery.
"""

import json
import os
from typing import Annotated
from urllib.parse import urlsplit

import typer

from lode import cli
from lode.cli import app
from lode.config import (
    CONFLUENCE_EMAIL_ENV,
    CONFLUENCE_TOKEN_ENV,
    JIRA_EMAIL_ENV,
    JIRA_TOKEN_ENV,
    AtlassianCredentials,
    Settings,
    resolve_confluence_credentials,
    resolve_jira_credentials,
)
from lode.confluence import HttpxConfluenceFetcher, fetch_confluence_page
from lode.drawdown import _CONFLUENCE_PAGE_RE, _JIRA_ISSUE_RE, _resolve_api_base
from lode.fetch_outcome import HttpOutcome, classify_http_status
from lode.jira_fetch import JiraHttpFetcher, fetch_jira_issue
from lode.webfetch import (
    Fetcher,
    FetchStatus,
    TooManyRedirectsError,
    TransientFetchError,
)

#: Current-user endpoints (confirmed shapes; see the ticket's design notes).
_JIRA_MYSELF_PATH = "/rest/api/3/myself"
_CONFLUENCE_CURRENT_USER_PATH = "/wiki/rest/api/user/current"

#: Issue-key / page-id extraction pattern per connector, reused from
#: lode.drawdown's own link-detection regexes (matched against a pasted URL's
#: path) so a JIRA/Confluence URL is parsed identically here and at capture
#: time.
_VERIFY_ID_PATTERNS = {"jira": _JIRA_ISSUE_RE, "confluence": _CONFLUENCE_PAGE_RE}


def _credential_source(env_var: str, config_value: str) -> str:
    """Which source resolved this credential half, for the printed report.

    Mirrors lode.config._resolve_atlassian_credentials's own env-var-primary,
    config.toml-fallback order -- this never resolves the value itself, only
    reports where it WOULD come from / did come from.
    """
    if os.environ.get(env_var):
        return f"env var {env_var}"
    if config_value:
        return "config.toml"
    return "unresolved"


def _default_verify_fetcher(
    connector: str, credentials: AtlassianCredentials, settings: Settings
) -> Fetcher:
    """Build the production current-user-probe fetcher for ``connector``.

    Reuses the connector's own thin HttpxFetcher subclass (Basic auth baked
    in at construction) rather than a bespoke httpx call -- the same fetcher
    is then reused for the optional content dry-run below.
    """
    if connector == "jira":
        return JiraHttpFetcher(credentials, settings)
    return HttpxConfluenceFetcher(credentials, settings)


def _resolve_verify_base_url(
    connector: str, configured_base: str, arg: str | None
) -> tuple[str | None, str]:
    """The base URL for the current-user GET, and where it came from.

    ``{connector}_base_url`` wins when set; otherwise it is derived from the
    optional positional arg via drawdown._resolve_api_base -- but only when
    that arg actually parses as an http(s) URL (a bare issue key/page id
    carries no host to derive a base from). Neither available -> (None,
    "unresolved"), the caller's cue to exit non-zero asking for one or the
    other, without making a network call.
    """
    if configured_base:
        return configured_base.rstrip("/"), f"configured ({connector}_base_url)"
    if arg and arg.strip():
        parts = urlsplit(arg.strip())
        if parts.scheme in ("http", "https") and parts.hostname:
            return _resolve_api_base(parts, ""), "inferred from the given URL"
    return None, "unresolved"


def _extract_verify_external_id(connector: str, arg: str) -> str | None:
    """Best-effort issue key / page id for the optional content dry-run.

    A pasted URL is matched against the connector's own link-detection
    pattern (the path shape lode.drawdown routes at capture time); anything
    else that isn't a URL at all is taken as a bare key/id verbatim. A URL
    that doesn't match the expected shape returns None -- the caller reports
    that the dry-run was skipped rather than guessing.
    """
    stripped = arg.strip()
    parts = urlsplit(stripped)
    if parts.scheme in ("http", "https") and parts.hostname:
        match = _VERIFY_ID_PATTERNS[connector].match(parts.path)
        return match.group(1) if match else None
    return stripped or None


def _run_verify(
    *,
    jira: bool,
    confluence: bool,
    arg: str | None,
    settings: Settings,
    fetcher: Fetcher | None = None,
) -> int:
    """Run the read-only preflight for one connector; returns the exit code.

    ``fetcher`` is the offline test seam (lode-04lz): when supplied, it
    stands in for the real HTTP transport for BOTH the current-user probe
    and the optional content dry-run below (the same object, reused --
    mirrors how fetch_jira_issue/fetch_confluence_page already accept
    ``fetcher=``); when omitted, a real connector-specific fetcher is built
    from the resolved credentials via ``cli._default_verify_fetcher`` --
    looked up through the package (rather than this module's own
    ``_default_verify_fetcher`` directly) because tests monkeypatch it as
    ``lode.cli._default_verify_fetcher`` (see ``lode.cli``'s own module
    docstring for why this indirection is needed). Never touches the DB,
    the job queue, or the vector store; the auth token is never read back
    off ``credentials`` here, so it can never reach stdout.
    """
    if jira == confluence:
        cli.console.print(
            "exactly one of --jira or --confluence is required",
            style="danger",
            markup=False,
            highlight=False,
        )
        return 1

    connector = "jira" if jira else "confluence"
    enabled = settings.jira_enabled if jira else settings.confluence_enabled
    email_env = JIRA_EMAIL_ENV if jira else CONFLUENCE_EMAIL_ENV
    token_env = JIRA_TOKEN_ENV if jira else CONFLUENCE_TOKEN_ENV
    config_email = settings.jira_email if jira else settings.confluence_email
    config_token = settings.jira_token if jira else settings.confluence_token
    configured_base = settings.jira_base_url if jira else settings.confluence_base_url
    credentials = (
        resolve_jira_credentials(settings)
        if jira
        else resolve_confluence_credentials(settings)
    )
    # Equivalent to jira_active/confluence_active(settings) by their own
    # definition (enabled AND creds resolve), but without re-resolving the
    # credentials already in hand above.
    active = enabled and credentials is not None

    cli.console.print(f"{connector}_enabled: {enabled}")
    email_source = _credential_source(email_env, config_email)
    token_source = _credential_source(token_env, config_token)
    if credentials is not None:
        cli.console.print(
            f"credentials: resolved -- email {credentials.email!r} "
            f"({email_source}), token ({token_source}, value redacted)",
            markup=False,
            highlight=False,
        )
    else:
        cli.console.print(
            f"credentials: unresolved -- email {email_source}, token {token_source}",
            markup=False,
            highlight=False,
        )
    cli.console.print(
        f"{connector}_base_url: "
        f"{configured_base or '(not set -- will infer from link)'}",
        markup=False,
        highlight=False,
    )

    if not active:
        reasons = []
        if not enabled:
            reasons.append(f"{connector}_enabled is False")
        if credentials is None:
            reasons.append("credentials are unresolved")
        cli.console.print(
            f"{connector} connector is inactive: " + "; ".join(reasons),
            style="danger",
            markup=False,
            highlight=False,
        )
        return 1

    base_url, base_source = _resolve_verify_base_url(connector, configured_base, arg)
    if base_url is None:
        cli.console.print(
            f"no base URL available -- set {connector}_base_url in config.toml, "
            f"or pass a sample {connector} URL as the positional argument",
            style="danger",
            markup=False,
            highlight=False,
        )
        return 1
    cli.console.print(
        f"base_url: {base_url} ({base_source})", markup=False, highlight=False
    )

    assert credentials is not None  # active() above already guarantees this
    probe_fetcher = fetcher or cli._default_verify_fetcher(
        connector, credentials, settings
    )
    myself_path = _JIRA_MYSELF_PATH if jira else _CONFLUENCE_CURRENT_USER_PATH
    myself_url = f"{base_url}{myself_path}"

    try:
        response = probe_fetcher.fetch(myself_url)
    except TransientFetchError as exc:
        cli.console.print(
            f"tenant unreachable right now: {exc}",
            style="danger",
            markup=False,
            highlight=False,
        )
        return 1
    except TooManyRedirectsError:
        cli.console.print(
            f"base URL misconfigured: too many redirects fetching {myself_url}",
            style="danger",
            markup=False,
            highlight=False,
        )
        return 1

    outcome = classify_http_status(response.status_code)
    if outcome is HttpOutcome.TRANSIENT:
        # Defensive: a conforming Fetcher already raises TransientFetchError
        # for this before returning a RawResponse; kept for a
        # non-conforming injected fetcher (e.g. a test stub).
        cli.console.print(
            f"tenant unreachable right now (http {response.status_code})",
            style="danger",
            markup=False,
            highlight=False,
        )
        return 1
    if outcome is HttpOutcome.TOMBSTONE:
        if response.status_code in (401, 403):
            cli.console.print(
                f"credentials rejected (http {response.status_code}) -- check "
                "the configured email/token",
                style="danger",
                markup=False,
                highlight=False,
            )
        elif response.status_code == 404:
            cli.console.print(
                f"endpoint not found (http 404) -- check {connector}_base_url",
                style="danger",
                markup=False,
                highlight=False,
            )
        else:
            cli.console.print(
                f"unexpected response (http {response.status_code}) from {myself_url}",
                style="danger",
                markup=False,
                highlight=False,
            )
        return 1

    try:
        display_name = json.loads(response.text).get("displayName") or "(unknown)"
    except json.JSONDecodeError:
        display_name = "(unknown)"
    cli.console.print(
        f"{connector} connector verified -- authenticated as {display_name!r}",
        style="ok",
        markup=False,
        highlight=False,
    )

    if arg and arg.strip():
        external_id = _extract_verify_external_id(connector, arg)
        if external_id is None:
            cli.console.print(
                f"content dry-run: could not parse an issue key/page id from {arg!r}",
                markup=False,
                highlight=False,
            )
        else:
            try:
                content_result = (
                    fetch_jira_issue(
                        external_id, base_url, fetcher=probe_fetcher, settings=settings
                    )
                    if jira
                    else fetch_confluence_page(
                        external_id, base_url, fetcher=probe_fetcher, settings=settings
                    )
                )
            except TransientFetchError as exc:
                # The content dry-run never changes the exit code (only the
                # auth probe does -- see the exit-code contract above); the
                # pure fetch units propagate TransientFetchError uncaught, so a
                # transient blip on this second call, after auth already
                # succeeded, is reported and shrugged off exactly like a
                # tombstone rather than crashing the command with a traceback.
                cli.console.print(
                    f"content dry-run ({external_id}): tenant unreachable "
                    f"right now ({exc})",
                    style="danger",
                    markup=False,
                    highlight=False,
                )
            else:
                if content_result.status is FetchStatus.OK:
                    cli.console.print(
                        f"content dry-run ({external_id}): OK",
                        style="ok",
                        markup=False,
                        highlight=False,
                    )
                else:
                    cli.console.print(
                        f"content dry-run ({external_id}): tombstoned "
                        f"({content_result.tombstone_reason})",
                        style="danger",
                        markup=False,
                        highlight=False,
                    )

    return 0


@app.command()
def verify(
    jira: Annotated[
        bool, typer.Option("--jira", help="Verify the JIRA Cloud connector.")
    ] = False,
    confluence: Annotated[
        bool,
        typer.Option("--confluence", help="Verify the Confluence Cloud connector."),
    ] = False,
    arg: Annotated[
        str | None,
        typer.Argument(
            metavar="[ISSUE_OR_PAGE]",
            help=(
                "Optional JIRA issue key/URL or Confluence page id/URL: also runs "
                "a read-only content dry-run, and doubles as the base-URL source "
                "when {connector}_base_url is not configured."
            ),
        ),
    ] = None,
) -> None:
    """Read-only preflight: confirm a JIRA/Confluence connector is configured and reachable.

    Exactly one of --jira / --confluence is required. Prints the resolved
    config state (enabled? credentials resolved, and from which source --
    env var vs config.toml, email shown, token always redacted? base URL
    configured or inferred?), then -- if the connector is active -- makes
    ONE authenticated, read-only GET to its current-user endpoint and
    reports the outcome. An optional positional issue key/URL (JIRA) or page
    id/URL (Confluence) additionally runs a read-only content dry-run via the
    existing pure fetch unit, proving actual content access, not just auth.

    Writes NOTHING: no DB rows, no job enqueue, no embedding, no
    vector-store writes -- and the auth token never appears in any output.

    Exit code 0 means verified reachable; any non-zero exit means
    misconfigured, unreachable, or auth-failed, so this is usable as a
    scriptable preflight gate.
    """
    raise typer.Exit(
        code=_run_verify(
            jira=jira, confluence=confluence, arg=arg, settings=cli._resolve_settings()
        )
    )

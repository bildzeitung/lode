"""Config-declared no_egress SCOPE rules (lode-35nu.11.8).

The per-row ``externals.no_egress`` flag (``lode.externals.set_no_egress``) can
only mark a resource that already has an ``externals`` row -- it structurally
cannot cover an external a tool has not fetched yet. A **scope** rule closes
that gap: a declarative, config-held predicate (``Settings.no_egress_scopes``)
evaluated against a candidate ``(external_id, source_type)`` pair at decision
time, live, with no row required and no write to ``externals`` ever performed.

Rules are declared in config, never materialized onto a row: adding one covers
every matching external immediately (already-captured or not), and removing
one un-withholds immediately, with no backfill/migration either way. This is
deliberate -- the decided design's whole point (``docs/decisions.md``,
``lode-35nu.11.8``).

**Not a generic seam.** ``no_egress`` is read by SQL ``JOIN`` at two call
sites -- :func:`lode.cited_answer._resolve_targets` and
:func:`lode.enrich._resolve_enrich_target` -- and a config predicate cannot
live inside a SQL join. :func:`is_no_egress_scoped` is the one shared
predicate; each site composes it with its own per-row flag itself (either
denying is a denial), rather than reimplementing the match.

**Confluence is out of scope (human decision, see docs/externals.md "No-egress
scope rules" and this ticket's notes).** ``drawdown.py``'s
``_CONFLUENCE_PAGE_RE`` persists only the numeric page id into
``external_id``; the space key is discarded at detection time and stored
nowhere, so a space-scoped rule has no space information to match against --
structurally unmatchable, not merely unimplemented. Rather than accept a
Confluence rule that can silently match nothing, :class:`NoEgressScopeRule`
**rejects** ``source_type="confluence"`` at config-load time with a clear
error (Settings validation) -- see the field validator on
:attr:`lode.config.Settings.no_egress_scopes`.
"""

from collections.abc import Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

#: Source types a scope rule may target. Confluence is deliberately absent --
#: rejected at config-load instead (see module docstring + config.py).
SCOPED_SOURCE_TYPES = ("jira", "web")


class NoEgressScopeRule(BaseModel):
    """One declarative no_egress scope rule.

    ``source_type`` selects which externals a rule can ever match --
    ``"jira"`` or ``"web"`` (Confluence is rejected at config-load, see the
    module docstring). ``match`` is interpreted per ``source_type``:

    - ``"jira"`` -- a JIRA project key (e.g. ``"PROJ"``), matched against the
      project-key prefix of a candidate JIRA issue key (``externals.external_id``
      for ``source_type="jira"`` is the issue key itself, e.g. ``"PROJ-123"``,
      per ``drawdown.py``'s ``_JIRA_ISSUE_RE``). Matched **case-insensitively**:
      that regex (``[A-Za-z][A-Za-z0-9]*-\\d+``) preserves whatever case the
      pasted URL used, so ``/browse/proj-123`` persists ``"proj-123"`` -- a
      case-sensitive compare against a rule written ``"PROJ"`` would silently
      fail to withhold it.
    - ``"web"`` -- a URL host (e.g. ``"example.atlassian.net"``), matched
      exactly against the **host** of a candidate URL (``externals.external_id``
      for ``source_type="web"`` IS the canonical URL). Host-only, not a
      host+path prefix -- the simplest shape that satisfies this ticket's
      acceptance; a path-prefix variant is a documented future option if ever
      needed, not built speculatively.

      Compared against ``urlsplit(...).hostname``, never ``netloc``: ``netloc``
      carries userinfo and a non-default port and preserves case, so
      ``https://user@Internal.Example.com:8443/x`` would not equal a rule
      ``"internal.example.com"`` and the content would be **sent**. That is a
      live leak on both paths -- ``drawdown.canonicalize_url`` retains a
      non-default port in the stored ``external_id``, and a candidate handed
      to this predicate before any row exists (the ticket's whole point) has
      been through no canonicalization at all. ``hostname`` lowercases and
      drops userinfo and port for us; a trailing root dot is stripped on both
      sides here.
    """

    model_config = ConfigDict(frozen=True)

    source_type: str
    match: str


def is_no_egress_scoped(
    external_id: str, source_type: str, rules: Sequence[NoEgressScopeRule]
) -> bool:
    """Whether ``(external_id, source_type)`` falls under any configured scope rule.

    Evaluated live against ``rules`` -- no ``externals`` row is required, which
    is the entire point: this is what lets a scope rule cover a not-yet-seen
    external. Returns ``False`` for any ``source_type`` a rule cannot be
    declared for at all (``"confluence"`` -- rejected earlier, at config-load;
    anything else unrecognized).

    **Fail-closed.** This predicate gates egress, so an evaluation that cannot
    be completed must never resolve to "allowed": if matching a rule raises
    (an unparseable candidate ``external_id``, say), the candidate is treated
    as scoped and withheld. Note the asymmetry that keeps that from withholding
    the world: a candidate is only ever parsed once a rule of its own
    ``source_type`` exists, so with no rules configured -- the default -- there
    is nothing to fail and nothing is withheld.
    """
    if source_type not in SCOPED_SOURCE_TYPES:
        return False
    for rule in rules:
        if rule.source_type != source_type:
            continue
        try:
            if source_type == "jira":
                prefix = f"{rule.match.upper()}-"
                candidate = external_id.upper()
                if candidate.startswith(prefix) and candidate[len(prefix) :].isdigit():
                    return True
            elif url_host(external_id) == _normalize_host(rule.match):
                return True
        except Exception:  # noqa: BLE001 -- fail CLOSED, see the docstring
            return True
    return False


def _normalize_host(host: str) -> str:
    """Lowercase ``host`` and strip the optional trailing root dot."""
    return host.strip().lower().rstrip(".")


def url_host(url: str) -> str:
    """The normalized host of ``url``, or ``""`` if it carries none.

    ``urlsplit(...).hostname`` -- not ``netloc`` -- so userinfo and port are
    dropped and the host is already lowercased; see
    :class:`NoEgressScopeRule`'s ``"web"`` note for why that distinction is a
    leak and not a nicety. ``""`` is returned for a hostless candidate rather
    than ``None`` so it can never compare equal to an empty rule ``match``,
    which :class:`lode.config.Settings` rejects at load in any case.
    """
    return _normalize_host(urlsplit(url).hostname or "")

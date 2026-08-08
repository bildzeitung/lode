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
sites -- :func:`lode.cited_answer._resolve_target` and
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

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

#: Source types a scope rule may target. Confluence is deliberately absent --
#: rejected at config-load instead (see module docstring + config.py).
_SCOPED_SOURCE_TYPES = ("jira", "web")


class NoEgressScopeRule(BaseModel):
    """One declarative no_egress scope rule.

    ``source_type`` selects which externals a rule can ever match --
    ``"jira"`` or ``"web"`` (Confluence is rejected at config-load, see the
    module docstring). ``match`` is interpreted per ``source_type``:

    - ``"jira"`` -- a JIRA project key (e.g. ``"PROJ"``), matched against the
      project-key prefix of a candidate JIRA issue key (``externals.external_id``
      for ``source_type="jira"`` is the issue key itself, e.g. ``"PROJ-123"``,
      per ``drawdown.py``'s ``_JIRA_ISSUE_RE``).
    - ``"web"`` -- a URL host (e.g. ``"example.atlassian.net"``), matched
      exactly against the host of a candidate URL (``externals.external_id``
      for ``source_type="web"`` IS the canonical URL). Host-only, not a
      host+path prefix -- the simplest shape that satisfies this ticket's
      acceptance; a path-prefix variant is a documented future option if ever
      needed, not built speculatively.
    """

    model_config = ConfigDict(frozen=True)

    source_type: str
    match: str


def is_no_egress_scoped(
    external_id: str, source_type: str, rules: list[NoEgressScopeRule]
) -> bool:
    """Whether ``(external_id, source_type)`` falls under any configured scope rule.

    Evaluated live against ``rules`` -- no ``externals`` row is required, which
    is the entire point: this is what lets a scope rule cover a not-yet-seen
    external. Returns ``False`` for any ``source_type`` a rule cannot be
    declared for at all (``"confluence"`` -- rejected earlier, at config-load;
    anything else unrecognized).
    """
    if source_type not in _SCOPED_SOURCE_TYPES:
        return False
    for rule in rules:
        if rule.source_type != source_type:
            continue
        if source_type == "jira":
            prefix = rule.match + "-"
            if external_id.startswith(prefix) and external_id[len(prefix) :].isdigit():
                return True
        elif source_type == "web":
            if urlsplit(external_id).netloc == rule.match:
                return True
    return False

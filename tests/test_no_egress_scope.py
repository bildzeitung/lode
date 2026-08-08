"""Tests for lode.no_egress_scope -- the shared no_egress SCOPE predicate (lode-35nu.11.8).

Exercises :func:`is_no_egress_scoped` directly, in isolation from any DB row --
which is the whole point of a scope rule: it must work for a candidate
``(external_id, source_type)`` that has no ``externals`` row at all.
"""

from lode.no_egress_scope import NoEgressScopeRule, is_no_egress_scoped


def test_jira_project_key_prefix_matches() -> None:
    rules = [NoEgressScopeRule(source_type="jira", match="PROJ")]
    assert is_no_egress_scoped("PROJ-123", "jira", rules) is True


def test_jira_scope_evaluates_with_no_externals_row() -> None:
    """The core acceptance case: a never-before-seen id under a scope is denied
    with no row involved at all -- the per-row flag structurally cannot cover
    this (it has nothing to flip)."""
    rules = [NoEgressScopeRule(source_type="jira", match="PROJ")]
    # No DB, no row, no externals table in sight -- pure string matching.
    assert is_no_egress_scoped("PROJ-999999", "jira", rules) is True


def test_jira_project_key_does_not_match_unrelated_prefix() -> None:
    rules = [NoEgressScopeRule(source_type="jira", match="PROJ")]
    assert is_no_egress_scoped("PROJECT2-1", "jira", rules) is False
    assert is_no_egress_scoped("OTHER-1", "jira", rules) is False


def test_jira_rule_does_not_match_web_source_type() -> None:
    rules = [NoEgressScopeRule(source_type="jira", match="PROJ")]
    assert is_no_egress_scoped("https://proj.example.com/x", "web", rules) is False


def test_web_host_matches_exactly() -> None:
    rules = [NoEgressScopeRule(source_type="web", match="internal.example.com")]
    assert is_no_egress_scoped("https://internal.example.com/a/b", "web", rules) is True


def test_web_host_does_not_match_subdomain_or_different_host() -> None:
    rules = [NoEgressScopeRule(source_type="web", match="internal.example.com")]
    assert (
        is_no_egress_scoped("https://sub.internal.example.com/a", "web", rules) is False
    )
    assert is_no_egress_scoped("https://other.example.com/a", "web", rules) is False


def test_no_rules_denies_nothing() -> None:
    assert is_no_egress_scoped("PROJ-1", "jira", []) is False
    assert is_no_egress_scoped("https://example.com/a", "web", []) is False


def test_confluence_source_type_never_matches() -> None:
    """Confluence is descoped (lode-35nu.11.8 human decision) -- the predicate
    itself refuses to match it too, as a backstop to config-load rejection
    (Settings' field validator is the primary enforcement point)."""
    rules = [NoEgressScopeRule(source_type="confluence", match="SPACE")]
    assert is_no_egress_scoped("12345", "confluence", rules) is False

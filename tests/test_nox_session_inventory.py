"""Regression test: the nox session inventory noxfile.py's own module
docstring and docs/onboarding.md describe must never drift from the actual
sessions (lode-dis6).

Two hand-maintained summaries of the same underlying fact drifted, in
different directions: noxfile.py's module docstring undercounted the opt-in
set by one (it omitted ``build``), and docs/onboarding.md undercounted it by
two (it omitted ``coverage`` and ``lock_currency``). A ticket about two
counts drifting is not one to fix by writing a third hand count -- the ground
truth here is derived MECHANICALLY, the same way lode-dis6 itself was: from
``nox.options.sessions`` (the default set) and the set of
``@nox.session``-decorated functions (every session that exists), both
parsed via AST rather than imported. AST, not an import, because importing
noxfile.py as a module would collide with tests/test_noxfile_venv_tool.py's
own "noxfile" entry in ``sys.modules`` (``load_module_from_path`` refuses a
second load under the same name, by design -- see that helper's docstring in
tests/conftest.py) -- and every value needed here is already a plain string
literal, so AST gives the same answer an import would, without the
collision.

This does not try to verify any specific prose *number* ("five opt-in
sessions") -- matching exact wording is brittle and not really the point.
Instead it asserts that every actual session's canonical invocation
(``nox -s <name>``, or ``nox -t <tag>`` for a tagged session like ``fix``) is
literally mentioned in both documents. That is the cheap, self-updating
mechanism that holds the restated fact true: add or remove a
``@nox.session`` function, or change ``nox.options.sessions``, and forget to
update one of the two docs, and this test fails there rather than silently
re-drifting.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NOXFILE_PATH = REPO_ROOT / "noxfile.py"
ONBOARDING_PATH = REPO_ROOT / "docs" / "onboarding.md"


def _is_nox_session_decorator(dec: ast.expr) -> bool:
    """Matches both ``@nox.session`` and ``@nox.session(tags=[...])``."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "session"


def _session_tag(dec: ast.expr) -> str | None:
    """The first string in a ``tags=[...]`` kwarg, if the decorator has one."""
    if not isinstance(dec, ast.Call):
        return None
    for kw in dec.keywords:
        if kw.arg == "tags" and isinstance(kw.value, ast.List) and kw.value.elts:
            first = kw.value.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    return None


def _session_invocations() -> dict[str, str]:
    """Map every ``@nox.session`` function name to its canonical CLI
    invocation -- ``nox -t <tag>`` for a tagged session (e.g. ``fix``),
    ``nox -s <name>`` otherwise. Derived from the decorator itself, not
    hand-listed, so a future tagged session is picked up automatically.
    """
    tree = ast.parse(NOXFILE_PATH.read_text())
    invocations: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not _is_nox_session_decorator(dec):
                continue
            tag = _session_tag(dec)
            invocations[node.name] = f"nox -t {tag}" if tag else f"nox -s {node.name}"
    return invocations


def _default_session_names() -> set[str]:
    """The literal ``nox.options.sessions = [...]`` list, parsed via AST."""
    tree = ast.parse(NOXFILE_PATH.read_text())
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "sessions"
            and isinstance(node.targets[0].value, ast.Attribute)
            and node.targets[0].value.attr == "options"
        ):
            continue
        assert isinstance(node.value, ast.List), (
            "nox.options.sessions is no longer a plain list literal -- update "
            "_default_session_names to match"
        )
        return {
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
    raise AssertionError("nox.options.sessions assignment not found in noxfile.py")


def test_derivation_matches_the_known_inventory_as_of_lode_dis6() -> None:
    """Guard the guard: if this fails, the AST derivation itself broke, not
    the inventory -- every assertion below would otherwise be vacuous."""
    invocations = _session_invocations()
    assert set(invocations) == {
        "fix",
        "tests",
        "shellcheck",
        "linkcheck",
        "unit",
        "lock_currency",
        "coverage",
        "build",
        "eval",
    }
    assert _default_session_names() == {"fix", "tests", "shellcheck", "linkcheck"}
    assert _default_session_names() <= set(invocations)


def test_noxfile_docstring_mentions_every_actual_session() -> None:
    docstring = ast.get_docstring(ast.parse(NOXFILE_PATH.read_text())) or ""
    invocations = _session_invocations()
    missing = {
        name: invocation
        for name, invocation in invocations.items()
        if invocation not in docstring
    }
    assert not missing, (
        f"noxfile.py's module docstring never mentions these sessions' "
        f"canonical invocation: {missing} -- lode-dis6"
    )


def test_onboarding_doc_mentions_every_actual_session() -> None:
    onboarding_text = ONBOARDING_PATH.read_text()
    invocations = _session_invocations()
    missing = {
        name: invocation
        for name, invocation in invocations.items()
        if invocation not in onboarding_text
    }
    assert not missing, (
        f"docs/onboarding.md never mentions these sessions' canonical "
        f"invocation: {missing} -- either add them, or make the doc say "
        f"outright that its session table is curated/partial (lode-dis6)"
    )

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
``@nox.session``-decorated functions (every session that exists). Both come
off conftest's shared ``noxfile_tree``/``nox_session_nodes``, which parse
noxfile.py rather than importing it -- see that section's comment in
tests/conftest.py for why an import is unavailable here.

Three checks, and the third is the one that covers the bug actually filed:

1. **Every real session is mentioned** in both documents, by its canonical
   invocation (``nox -s <name>``, or ``nox -t <tag>`` for a tagged session
   like ``fix``). Add a session and forget a doc, and this fails.
2. **Every mentioned invocation is real** -- the converse, which catches the
   drift direction (1) cannot see: *removing* a session while its line, table
   row, or prose mention stays behind.
3. **A stated opt-in COUNT must equal the derived one.** (1) and (2) both
   pass while the prose says "four opt-in sessions" above a list of five --
   which is exactly what lode-dis6 was filed for.

Check (3) is the only one that has to touch wording, and it is deliberately
set one notch stricter than "verify any count you happen to state": it
requires each document to state a count in the form ``<N> opt-in sessions``.
The looser form goes silently vacuous the moment someone rephrases the
sentence while still counting ("five of the sessions are opt-in") -- a
reworded claim drifts exactly like the original did, and this file exists
because that already happened twice. The cost is that deleting the count
outright -- a legitimate fix, since an unstated count cannot drift -- also
turns this red; the failure message names that as one of its three ways out.

What is deliberately NOT checked: the DEFAULT-set count ("all FOUR sessions",
"four sessions total"), whose two phrasings differ per file. It is covered
indirectly and almost always -- changing the default set changes the opt-in
count too, since opt-in is derived as (all sessions - default sessions), so
check (3) fires. The one blind spot left is a change that adds a session AND
promotes one into the default set in the same edit, holding the opt-in count
at five while "FOUR" goes stale.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import nox_session_nodes, noxfile_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
NOXFILE_PATH = REPO_ROOT / "noxfile.py"
ONBOARDING_PATH = REPO_ROOT / "docs" / "onboarding.md"

# Any ``nox -s <name>`` / ``nox -t <tag>`` in prose, however it is quoted or
# emphasized around -- markdown backticks and rST double-backticks both sit
# outside the match. No capture group, so ``findall`` yields whole
# invocations, directly comparable to the ones derived below.
_INVOCATION_RE = re.compile(r"nox -[st] [A-Za-z_][\w-]*")

# A count claim of the form "<N> opt-in sessions". The token before the noun
# phrase is only treated as a claim when it is actually a number, so "the
# opt-in sessions" is prose, not a miscount.
_OPT_IN_COUNT_RE = re.compile(r"([\w-]+)\s+opt-in\s+sessions", re.IGNORECASE)

# Enough to name any plausible session count in words. A doc that outgrows
# this reads as "no count stated" and fails check (3) with its
# claim-is-missing message rather than a count mismatch -- loud either way,
# but extend the map rather than leaving the next reader to work that out.
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _as_number(token: str) -> int | None:
    """``"five"`` / ``"5"`` -> 5; anything else -> None (not a count claim)."""
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token.lower())


def _noxfile_docstring() -> str:
    return ast.get_docstring(noxfile_tree(NOXFILE_PATH)) or ""


def _onboarding_text() -> str:
    return ONBOARDING_PATH.read_text(encoding="utf-8")


# The two documents every check below runs against. Parametrized rather than
# looped so one run reports BOTH when both are wrong -- which is the state
# lode-dis6 was filed in.
_DOCUMENTS: dict[str, Callable[[], str]] = {
    "noxfile.py's module docstring": _noxfile_docstring,
    "docs/onboarding.md": _onboarding_text,
}


def _session_tags(node: ast.FunctionDef) -> list[str]:
    """The strings in this session's ``@nox.session(tags=[...])``, if any."""
    for dec in node.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        if dec.func.attr != "session":
            continue
        for kw in dec.keywords:
            if kw.arg == "tags" and isinstance(kw.value, ast.List):
                return [
                    elt.value
                    for elt in kw.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
    return []


def _session_invocations() -> dict[str, str]:
    """Map every ``@nox.session`` function name to its canonical CLI
    invocation -- ``nox -t <tag>`` for a tagged session (e.g. ``fix``),
    ``nox -s <name>`` otherwise. Derived from the decorator itself, not
    hand-listed, so a future tagged session is picked up automatically.
    """
    invocations = {}
    for name, node in nox_session_nodes(NOXFILE_PATH).items():
        tags = _session_tags(node)
        invocations[name] = f"nox -t {tags[0]}" if tags else f"nox -s {name}"
    return invocations


def _real_invocations() -> set[str]:
    """Every invocation string that names something real: ``nox -s <session>``
    for each session, plus ``nox -t <tag>`` for each tag one carries. Wider
    than the canonical set above on purpose -- a tagged session is genuinely
    reachable both ways, so a document naming either is not stale.
    """
    nodes = nox_session_nodes(NOXFILE_PATH)
    real = {f"nox -s {name}" for name in nodes}
    for node in nodes.values():
        real |= {f"nox -t {tag}" for tag in _session_tags(node)}
    return real


def _default_session_names() -> set[str]:
    """The literal ``nox.options.sessions = [...]`` list, parsed via AST."""
    for node in ast.walk(noxfile_tree(NOXFILE_PATH)):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if ast.unparse(node.targets[0]) != "nox.options.sessions":
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


def test_the_derivation_is_not_vacuous() -> None:
    """The tripwire under every check below: a parse that silently matched
    nothing would make them all vacuously true.

    Deliberately structural rather than a pinned list of the nine session
    names that exist today. A hand-written inventory here would be a THIRD
    copy of the very thing this file exists to stop anyone hand-maintaining,
    and would make every legitimate session addition a three-file edit. What
    is asserted instead never goes stale: both derivations found something,
    the two entry points CLAUDE.md names as the merge gate exist, and the
    default set is a subset of all sessions -- without which the opt-in
    count, derived by subtraction, would be meaningless.
    """
    invocations = _session_invocations()
    defaults = _default_session_names()
    assert invocations, f"no @nox.session functions found in {NOXFILE_PATH}"
    assert defaults, f"nox.options.sessions parsed as empty in {NOXFILE_PATH}"
    assert {"fix", "tests"} <= set(invocations), (
        "noxfile.py no longer defines the two sessions CLAUDE.md names as the "
        "merge gate -- either the parse broke, or this is a much bigger change "
        "than a docstring edit"
    )
    assert defaults <= set(invocations), (
        f"nox.options.sessions names {sorted(defaults - set(invocations))}, "
        "which is not a @nox.session function"
    )


@pytest.mark.parametrize("label", _DOCUMENTS)
def test_document_mentions_every_actual_session(label: str) -> None:
    text = _DOCUMENTS[label]()
    missing = {
        name: invocation
        for name, invocation in _session_invocations().items()
        if invocation not in text
    }
    assert not missing, (
        f"{label} never mentions these sessions' canonical invocation: "
        f"{missing} -- lode-dis6"
    )


@pytest.mark.parametrize("label", _DOCUMENTS)
def test_document_mentions_no_session_that_no_longer_exists(label: str) -> None:
    """The converse of the check above, and the only one that sees a REMOVED
    session whose line, table row, or prose mention stayed behind."""
    stale = set(_INVOCATION_RE.findall(_DOCUMENTS[label]())) - _real_invocations()
    assert not stale, (
        f"{label} still invokes {sorted(stale)}, which noxfile.py no longer "
        f"defines -- a removed session left a stale mention behind (lode-dis6)"
    )


@pytest.mark.parametrize("label", _DOCUMENTS)
def test_stated_opt_in_count_matches_the_derived_one(label: str) -> None:
    """The check that covers the bug lode-dis6 was actually filed for: every
    individual session can be listed correctly while the sentence above the
    list still says "four"."""
    expected = len(_session_invocations()) - len(_default_session_names())
    claimed = [
        n
        for token in _OPT_IN_COUNT_RE.findall(_DOCUMENTS[label]())
        if (n := _as_number(token)) is not None
    ]
    assert claimed, (
        f"{label} no longer states how many opt-in sessions there are in the "
        f"form '<N> opt-in sessions'. Three ways out, pick deliberately: put "
        f"the count back; point _OPT_IN_COUNT_RE at the new wording if the "
        f"claim was merely rephrased; or delete this check outright if the "
        f"count was dropped on purpose, since an unstated count cannot drift "
        f"(lode-dis6)"
    )
    assert all(n == expected for n in claimed), (
        f"{label} claims {claimed} opt-in session(s); noxfile.py actually has "
        f"{expected} (every @nox.session function minus nox.options.sessions) "
        f"-- lode-dis6"
    )

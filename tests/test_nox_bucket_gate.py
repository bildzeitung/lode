"""Registration gate for the blessed nox gate-invocation buckets (lode-6ldh).

lode-vvt1 fixed a shellcheck session that had been silently missing from every
gate list hand-typed in instruction-file prose. lode-6ldh's fix is structural,
not another hand list: every covered ``@nox.session`` function must carry
EXACTLY ONE of three blessed tags -- ``fix`` (unchanged), ``tests``, or
``everything-else`` -- so instruction files invoke gates by TAG (``nox -t
tests``, ``nox -t everything-else``) rather than enumerating session names,
and a session that forgets its tag (or picks up two) turns this test red
instead of quietly falling through every bucket.

Parsed rather than imported, via the shared ``nox_session_nodes``/
``noxfile_tree`` helpers in ``tests/conftest.py`` -- see that module's own
comment for why importing ``noxfile.py`` a second time is unavailable here
(nox's global session registry, ``test_noxfile_venv_tool.py``'s
``_load_noxfile``).

**Scope (deliberately not exhaustive over every ``@nox.session``).** Four
opt-in, CI-only/packaging sessions -- ``eval``, ``coverage``, ``build``,
``lock_currency`` -- are invoked directly by name in their own narrow
contexts (GitHub workflows, ``/land``'s explicit ``lock_currency`` call),
never through the instruction-file gate prose this bucket scheme exists to
keep honest, and their own docstrings already say so (``noxfile.py``'s module
docstring, "kept OUT of the default nox session set"). They are named in
``_EXEMPT_SESSIONS`` below rather than silently absent from any check, so
extending the exemption is a visible, one-line diff -- and adding a NEW
session without deciding whether it's bucketed or exempt is a green run only
by accident: the non-vacuity test below fails if ``_EXEMPT_SESSIONS`` ever
drifts to cover every real session, since that would make the bucket-coverage
checks vacuously pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conftest import nox_session_nodes

REPO_ROOT = Path(__file__).resolve().parent.parent
NOXFILE_PATH = REPO_ROOT / "noxfile.py"

# The three blessed tags -- exactly one of these per COVERED session.
_BLESSED_TAGS = {"fix", "tests", "everything-else"}

# CI-only/packaging sessions the bucket scheme deliberately does not cover
# (see module docstring). Each entry names the session so a change here is a
# reviewable, one-line diff -- never a silent broadening.
_EXEMPT_SESSIONS = {"eval", "coverage", "build", "lock_currency"}


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


def _covered_sessions() -> dict[str, list[str]]:
    """name -> tags, for every ``@nox.session`` NOT in ``_EXEMPT_SESSIONS``."""
    return {
        name: _session_tags(node)
        for name, node in nox_session_nodes(NOXFILE_PATH).items()
        if name not in _EXEMPT_SESSIONS
    }


def test_the_derivation_is_not_vacuous() -> None:
    """Tripwire: both the covered set and the exempt set must actually match
    something real, or every check below would pass on an empty parse."""
    all_sessions = set(nox_session_nodes(NOXFILE_PATH))
    covered = _covered_sessions()
    assert all_sessions, f"no @nox.session functions found in {NOXFILE_PATH}"
    assert _EXEMPT_SESSIONS <= all_sessions, (
        f"_EXEMPT_SESSIONS names {sorted(_EXEMPT_SESSIONS - all_sessions)}, "
        "which noxfile.py no longer defines -- update the exemption list"
    )
    assert covered, (
        "every @nox.session is in _EXEMPT_SESSIONS -- the bucket-coverage "
        "checks below would be vacuously true. Either a session was wrongly "
        "exempted, or this test's exemption list itself has drifted too wide."
    )
    assert {"fix", "tests", "shellcheck"} <= set(covered), (
        "the three sessions this gate was written to protect "
        "(fix/tests/shellcheck) are missing from the covered set -- the "
        "parse broke, or _EXEMPT_SESSIONS grew to swallow them"
    )


def test_every_covered_session_carries_exactly_one_blessed_tag() -> None:
    """The core registration gate: zero or 2+ blessed tags both fail."""
    violations = {
        name: [t for t in tags if t in _BLESSED_TAGS]
        for name, tags in _covered_sessions().items()
    }
    bad = {
        name: matched
        for name, matched in violations.items()
        if len(matched) != 1
    }
    assert not bad, (
        "these @nox.session functions carry zero or 2+ of the blessed tags "
        f"{sorted(_BLESSED_TAGS)} (lode-6ldh): {bad}. Every session not in "
        "_EXEMPT_SESSIONS must carry EXACTLY ONE of 'fix', 'tests', or "
        "'everything-else' so instruction-file gate prose can invoke it by "
        "tag instead of enumerating session names."
    )


def test_tests_bucket_has_both_the_full_and_fast_view() -> None:
    """``tests`` and ``unit`` are the two VIEWS of one bucket the staged gate
    policy relies on (builders run the fast ``unit`` view; the reviewer and
    ``/land``'s re-gate run the full ``tests`` view) -- both must actually
    carry the ``tests`` tag, or the staged policy's builder-side invocation
    silently stops being part of the bucket it claims to be."""
    covered = _covered_sessions()
    for name in ("tests", "unit"):
        assert name in covered, f"expected a {name!r} nox session, none found"
        assert "tests" in covered[name], (
            f"{name!r} no longer carries the 'tests' tag -- the staged gate "
            "policy (lode-6ldh) treats 'tests' and 'unit' as two views of "
            "ONE bucket, not two independently-bucketed sessions"
        )


def test_everything_else_bucket_covers_the_four_offline_default_sessions() -> None:
    """The four default-set sessions besides fix/tests -- exactly the ones
    lode-vvt1's shellcheck omission was about -- must all land in the
    everything-else bucket."""
    covered = _covered_sessions()
    expected = {"shellcheck", "linkcheck", "docstringcheck", "docs"}
    for name in expected:
        assert name in covered, f"expected a {name!r} nox session, none found"
        assert "everything-else" in covered[name], (
            f"{name!r} is missing the 'everything-else' tag (lode-6ldh)"
        )

"""Regression tests for ``noxfile.py``'s ``_venv_tool`` (lode-0yfn).

The hazard: ``noxfile.py`` sets ``default_venv_backend = "none"``, so every
nox session inherits whatever PATH the invoking shell has. A session that
shells out to a tool by bare name (``"ruff"``, ``"pytest"``, ...) then
resolves whichever copy is *earliest* on that PATH. On a box with a stale
system-wide install sitting ahead of the project's own ``./venv/bin``
(observed: an ambient ``~/.local/bin/ruff`` 0.15.11 shadowing the venv's
then-pinned ``ruff==0.15.22``, lode-umh2), this silently runs a DIFFERENT
tool than the one ``pyproject.toml``/``requirements.lock`` pins, while the
gate still reports success.

``_venv_tool`` fixes this by resolving straight to the tool's known on-disk
location under the project's own ``./venv/bin`` rather than searching PATH at
all. These tests import the ACTUAL ``noxfile.py`` (never a reimplementation,
per the lode-verb sabotage-provable bar -- see ``tests/test_code_concurrency_cap.py``
and ``tests/test_source_tree_guard.py`` for the same pattern applied to other
repo tooling) and reconstruct the exact shadowing hazard: a decoy executable
placed earlier on PATH than the (faked, for isolation) venv bin dir.

Two halves, and the second is the one that keeps the fix alive: the tests
above ``_DEV_EXTRA_TOOLS`` prove ``_venv_tool`` resolves correctly, and the
ones below prove every session that needs it actually uses it -- so a future
edit reintroducing ``session.run("pytest", ...)`` fails here rather than
quietly restoring the original bug.
"""

from __future__ import annotations

import ast
import functools
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent
NOXFILE_PATH = REPO_ROOT / "noxfile.py"


@functools.cache
def _load_noxfile() -> ModuleType:
    """Import the real ``noxfile.py`` by explicit path, once per test session.

    It lives at the repo root, one level above ``tests/``, so it is not
    reachable by name -- the shared helper (tests/conftest.py) loads it from
    its file path instead.

    The cache is load-bearing, not a micro-optimization: executing the module
    runs its ``@nox.session`` decorators, which register into nox's *global*
    session registry. Executing it a second time re-registers every session
    and nox emits ``FutureWarning: The session '<name>' has already been
    registered; this will be an error in a future version of nox`` -- i.e.
    an uncached loader would turn this file red on a future nox upgrade.
    Removing the cache now fails immediately rather than waiting for that
    release, because the shared helper asserts the name is not already
    resident in ``sys.modules`` and a second load trips that first.
    Callers that mutate the module (``monkeypatch.setattr(noxfile, ...)``)
    are unaffected by sharing one instance: monkeypatch reverts per test.
    """
    return load_module_from_path("noxfile", NOXFILE_PATH)


class _FakeSession:
    """Minimal stand-in for ``nox.Session``'s ``.error()`` contract.

    ``_venv_tool`` only ever touches ``session.error`` on its failure path,
    so faking that one collaborator method (real nox raises ``_SessionQuit``
    from it; this raises ``RuntimeError`` with the same message) is enough to
    observe both that it was called and that ``_venv_tool`` never returns
    normally afterward -- constructing a real ``nox.Session`` needs a full
    runner/config that has nothing to do with what's under test here.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)
        raise RuntimeError(message)


def test_venv_tool_resolves_the_real_projects_ruff() -> None:
    """Sanity check against the actual, currently-built project venv."""
    noxfile = _load_noxfile()
    session = _FakeSession()

    resolved = noxfile._venv_tool(session, "ruff")

    assert resolved == str(REPO_ROOT / "venv" / "bin" / "ruff")
    assert Path(resolved).is_file()
    assert not session.errors


def test_venv_tool_ignores_a_shadowing_earlier_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lode-0yfn hazard, reconstructed directly.

    A decoy ``widget`` executable sits on PATH ahead of a (faked) venv bin
    dir containing the real one -- exactly the shape of an ambient
    ``~/.local/bin/ruff`` preceding ``./venv/bin`` on a real box. A bare-name
    PATH search (``shutil.which``) finds the decoy first, proving the
    shadowing setup is real; ``_venv_tool`` must resolve the venv's copy
    regardless, and running the path it returns must produce the REAL
    output, never the DECOY's.
    """
    noxfile = _load_noxfile()

    fake_venv_bin = tmp_path / "fake_venv" / "bin"
    fake_venv_bin.mkdir(parents=True)
    real_tool = fake_venv_bin / "widget"
    real_tool.write_text("#!/bin/sh\necho REAL\n")
    real_tool.chmod(0o755)
    monkeypatch.setattr(noxfile, "_VENV_BIN", fake_venv_bin)

    decoy_dir = tmp_path / "decoy_bin"
    decoy_dir.mkdir()
    decoy_tool = decoy_dir / "widget"
    decoy_tool.write_text("#!/bin/sh\necho DECOY\n")
    decoy_tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{decoy_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    # Prove the shadowing hazard is real before asserting the fix defeats it.
    assert shutil.which("widget") == str(decoy_tool)

    session = _FakeSession()
    resolved = noxfile._venv_tool(session, "widget")

    assert resolved == str(real_tool)
    assert not session.errors

    output = subprocess.run(
        [resolved], capture_output=True, text=True, check=True
    ).stdout
    assert output.strip() == "REAL"


def test_venv_tool_fails_loudly_when_the_tool_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing project venv (or missing tool in it) errors, never silently
    falls through to whatever happens to be on ambient PATH."""
    noxfile = _load_noxfile()
    monkeypatch.setattr(noxfile, "_VENV_BIN", tmp_path)  # empty dir -- nothing here

    session = _FakeSession()
    with pytest.raises(RuntimeError) as exc_info:
        noxfile._venv_tool(session, "ruff")

    assert session.errors  # session.error() was actually invoked, not skipped
    message = str(exc_info.value)
    assert "ruff" in message
    assert "python-init.sh" in message


# Tools the ``dev`` extra installs into ``./venv/bin``, i.e. the ones a
# stale system-wide copy can shadow. Naming any of these as the first
# argument to ``session.run`` is the exact regression _venv_tool exists to
# prevent.
_DEV_EXTRA_TOOLS = frozenset({"ruff", "pytest", "shellcheck", "python", "python3"})

# Sessions that shell out to a tool OUTSIDE ./venv on purpose, so the guard
# below must not apply to them. Keeping this as an explicit set is the
# mechanism: a future session added to noxfile.py is guarded by default, and
# exempting it is a deliberate edit here rather than an omission nobody sees.
_EXEMPT_SESSIONS = frozenset({"build", "lock_currency"})


def _session_nodes() -> dict[str, ast.FunctionDef]:
    """Every ``@nox.session``-decorated function in the real ``noxfile.py``.

    Parsed rather than string-sliced: an earlier form of these tests located
    each session by ``source.index("\\n@nox.session\\ndef <next-one>(")``,
    which silently depended on the *order* sessions happen to appear in and
    had to be updated whenever one moved. The AST asks the question directly.
    """

    def is_nox_session(dec: ast.expr) -> bool:
        # Matches both ``@nox.session`` and ``@nox.session(tags=[...])``.
        target = dec.func if isinstance(dec, ast.Call) else dec
        return isinstance(target, ast.Attribute) and target.attr == "session"

    tree = ast.parse(NOXFILE_PATH.read_text())
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(is_nox_session(dec) for dec in node.decorator_list)
    }


def _bare_tool_names(node: ast.FunctionDef) -> list[str]:
    """Dev-extra tools this session passes to ``session.run`` by bare name."""
    found = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "run"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "session"):
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and first.value in _DEV_EXTRA_TOOLS:
            found.append(first.value)
    return found


def test_no_session_shells_out_to_a_dev_extra_tool_by_bare_name() -> None:
    """The self-enforcing half of lode-0yfn.

    The rest of this file proves ``_venv_tool`` resolves correctly; this
    proves every session that needs it actually *uses* it, so the fix cannot
    silently rot. Without this, a future session (or an edit to an existing
    one) could reintroduce ``session.run("pytest", ...)`` and nothing would
    fail -- the guard would depend on the next author remembering it, which
    is precisely how the original bug survived as long as it did.
    """
    sessions = _session_nodes()
    # Guard the guard: a parser that silently matched nothing would make
    # every assertion below vacuously true.
    assert sessions, f"no @nox.session functions found in {NOXFILE_PATH}"
    assert _EXEMPT_SESSIONS <= set(sessions), (
        f"exempt sessions {sorted(_EXEMPT_SESSIONS - set(sessions))} no longer "
        "exist in noxfile.py -- update _EXEMPT_SESSIONS"
    )

    offenders = {
        name: bare
        for name, node in sessions.items()
        if name not in _EXEMPT_SESSIONS and (bare := _bare_tool_names(node))
    }
    assert not offenders, (
        f"these nox sessions shell out to a dev-extra tool by bare name: "
        f"{offenders}. A stale system-wide copy earlier on PATH would shadow "
        "the pinned one and produce a falsely-green gate (lode-0yfn) -- pass "
        "_venv_tool(session, <name>) instead, or add the session to "
        "_EXEMPT_SESSIONS if it genuinely means to use an ambient tool."
    )


@pytest.mark.parametrize("name", sorted(_EXEMPT_SESSIONS))
def test_exempt_sessions_deliberately_do_not_use_venv_tool(name: str) -> None:
    """``build`` shells out to ambient ``python -m build`` on purpose
    (``build.yml``/``release.yml`` run it with no ``./venv`` at all);
    ``lock_currency`` resolves ``uv``, a system-wide tool never installed
    into ``./venv``, already failing closed if absent (lode-sys4). Both must
    stay untouched -- if one starts using ``_venv_tool``, that is a real
    decision and this test should be updated alongside it, not silently.
    """
    node = _session_nodes()[name]
    used = [
        n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == "_venv_tool"
    ]
    assert not used, f"{name} is in _EXEMPT_SESSIONS but now calls _venv_tool"

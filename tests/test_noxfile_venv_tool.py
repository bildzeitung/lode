"""Regression tests for ``noxfile.py``'s ``_venv_tool`` (lode-0yfn).

The hazard: ``noxfile.py`` sets ``default_venv_backend = "none"``, so every
nox session inherits whatever PATH the invoking shell has. A session that
shells out to a tool by bare name (``"ruff"``, ``"pytest"``, ...) -- or a
cwd-relative fragment like ``"./venv/bin/ruff"`` -- then resolves whichever
copy is *earliest* on that PATH, or breaks outright when invoked from a
different cwd. On a box with a stale system-wide install sitting ahead of
the project's own ``./venv/bin`` (observed: an ambient ``~/.local/bin/ruff``
0.15.11 shadowing the venv's then-pinned ``ruff==0.15.22``, lode-umh2), this
silently runs a DIFFERENT tool than the one ``pyproject.toml``/
``requirements.lock`` pins, while the gate still reports success.

``_venv_tool`` fixes this by resolving straight to the tool's known on-disk
location under the project's own ``./venv/bin`` rather than searching PATH at
all. These tests import the ACTUAL ``noxfile.py`` (never a reimplementation,
per the lode-verb sabotage-provable bar -- see ``tests/test_code_concurrency_cap.py``
and ``tests/test_source_tree_guard.py`` for the same pattern applied to other
repo tooling) and reconstruct the exact shadowing hazard: a decoy executable
placed earlier on PATH than the (faked, for isolation) venv bin dir.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NOXFILE_PATH = REPO_ROOT / "noxfile.py"


def _load_noxfile() -> ModuleType:
    """Import the real ``noxfile.py`` by explicit path.

    Not on ``sys.path`` by default (it lives at the repo root, one level
    above ``tests/``), so ``importlib`` loads it directly rather than relying
    on pytest's rootdir-insertion import mode to have put it there.
    """
    spec = importlib.util.spec_from_file_location("noxfile", NOXFILE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_build_session_deliberately_does_not_use_venv_tool() -> None:
    """The ``build`` session shells out to ambient ``python -m build`` on
    purpose (its own docstring, and ``build.yml``/``release.yml``, which run
    it with no ``./venv`` at all) -- it must stay untouched by this fix."""
    source = NOXFILE_PATH.read_text()
    build_start = source.index("\ndef build(session")
    build_end = source.index("\n@nox.session\ndef eval(", build_start)
    build_body = source[build_start:build_end]

    assert '"python", "-m", "build"' in build_body
    assert "_venv_tool" not in build_body


def test_lock_currency_session_deliberately_does_not_use_venv_tool() -> None:
    """The ``lock_currency`` session resolves ``uv`` -- a separate,
    system-wide tool never installed into ``./venv`` -- via ``shutil.which``,
    already failing closed if absent (lode-sys4). It must stay untouched by
    this fix, which only targets tools the ``dev`` extra installs."""
    source = NOXFILE_PATH.read_text()
    session_start = source.index("\ndef lock_currency(session")
    session_end = source.index("\n@nox.session\ndef coverage(", session_start)
    session_body = source[session_start:session_end]

    assert 'shutil.which("uv")' in session_body
    assert "_venv_tool" not in session_body

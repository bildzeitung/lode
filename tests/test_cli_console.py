"""Tests for the CLI's shared rich ``Console`` (lode-l38d.1) — specifically
the ``NO_COLOR`` detection mechanism, and the SUBPROCESS pattern required to
actually test it.

lode-xgaa: a technical review of lode-l38d.1 found that the mechanism
originally recorded as guidance on lode-l38d.1 (and implicitly relied on by
the colour children lode-l38d.4/.5/.6/.10) — "colour is off for free under
test because CliRunner's captured output is never a TTY" — is FALSE.

rich's ``Console()`` freezes BOTH its TTY check and its ``NO_COLOR`` read at
CONSTRUCTION time (``rich.console.Console.__init__``: ``self.no_color =
no_color if no_color is not None else self._environ.get("NO_COLOR", "") !=
""``, evaluated exactly once and stored as a plain ``bool``). At module
scope — ``console = Console()`` in ``src/lode/cli.py`` — that construction
happens at IMPORT time, not per-invocation. Two consequences:

* Colour is off under ``typer.testing.CliRunner`` today only because
  *pytest's own default output capture* had already replaced ``sys.stdout``
  before ``lode.cli`` was first imported (typically at collection) — NOT
  because of anything ``CliRunner`` itself does. Run the suite as
  ``pytest -s`` from a real terminal and the import-time TTY check freezes
  the other way.
* ``monkeypatch.setenv("NO_COLOR", "1")`` AFTER ``lode.cli`` is already
  imported is a silent no-op: the env read already happened at import, so
  such an assertion passes WITHOUT exercising the ``NO_COLOR`` path at all.

The only way to actually exercise ``NO_COLOR`` detection is a FRESH
SUBPROCESS that imports ``lode.cli`` with the target env already set, so the
shared ``console`` re-detects from scratch. This module is that pattern,
manually proven non-vacuous while building lode-xgaa: with
``_console_no_color``'s subprocess ``-c`` script temporarily patched so the
printed value ignores the actual ``no_color`` attribute (i.e. the harness
itself broken), ``test_console_no_color_true_when_env_set`` failed as
expected; reverted, it passes again. Any colour ticket
(lode-l38d.4/.5/.6/.10) that asserts the ``NO_COLOR`` negative path should
copy this pattern rather than reinvent — or silently no-op — one of its own.
See also ``src/lode/cli.py``'s ``console`` docstring and docs/stack.md's
``rich`` row, which record the same mechanism.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _console_no_color(*, no_color_env: str | None) -> bool:
    """Report ``lode.cli.console.no_color`` from a FRESH subprocess.

    A fresh ``python -c`` subprocess re-imports ``lode.cli`` from scratch, so
    the shared module-level ``Console()`` re-reads ``NO_COLOR`` at
    construction under an env this helper controls precisely — the only way
    to exercise this path, which nothing in-process can do (see module
    docstring).
    """
    env = dict(os.environ)
    if no_color_env is None:
        env.pop("NO_COLOR", None)
    else:
        env["NO_COLOR"] = no_color_env
    result = subprocess.run(
        [sys.executable, "-c", "import lode.cli; print(lode.cli.console.no_color)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip() == "True"


def test_console_no_color_true_when_env_set() -> None:
    """``NO_COLOR=1`` in a subprocess's env freezes the freshly-imported
    ``console.no_color`` to ``True``."""
    assert _console_no_color(no_color_env="1") is True


def test_console_no_color_false_when_env_absent() -> None:
    """Control for the assertion above: with ``NO_COLOR`` unset, ``no_color``
    freezes to ``False``.

    This control is what makes the test above non-vacuous. Without it, a
    broken harness that always reports ``True`` regardless of env (or a
    subprocess that silently failed to actually re-detect) would still make
    the positive-path test pass — exactly the silent-no-op failure mode
    lode-xgaa exists to prevent.
    """
    assert _console_no_color(no_color_env=None) is False

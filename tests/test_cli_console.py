"""Tests for the CLI's shared rich ``Console`` (lode-l38d.1) — specifically
the ``NO_COLOR`` detection mechanism, and the SUBPROCESS pattern required to
actually test it.

lode-xgaa: a technical review of lode-l38d.1 found that the mechanism
originally recorded as guidance on lode-l38d.1 (and implicitly relied on by
the colour children lode-l38d.4/.5/.6/.10) — "colour is off for free under
test because CliRunner's captured output is never a TTY" — is FALSE.

rich's ``Console()`` freezes ``color_system`` (and ``no_color``/
``is_interactive``) at CONSTRUCTION time — ``color_system`` is computed once
in ``Console.__init__`` FROM ``is_terminal`` at that moment and stored as a
plain attribute; ``self.no_color = no_color if no_color is not None else
self._environ.get("NO_COLOR", "") != ""`` is likewise evaluated exactly once
and stored as a plain ``bool``. ``is_terminal`` itself is NOT frozen — it
stays a live property that keeps re-reading ``os.environ`` on every access —
but it no longer gates COLOUR once ``color_system`` is fixed, and
``color_system`` alone decides whether any ANSI style is emitted. (It does
still gate control codes; it is only the colour question it stops answering.
Executed verification, with rich source-line refs: ``tests/conftest.py``'s
scrub comment.) At module scope —
``console = Console()`` in ``src/lode/cli.py`` — that construction happens at
IMPORT time, not per-invocation. Two consequences:

* Colour is off under ``typer.testing.CliRunner`` today only because
  *pytest's own default output capture* had already replaced ``sys.stdout``
  before ``lode.cli`` was first imported (typically at collection) — NOT
  because of anything ``CliRunner`` itself does. Run the suite as
  ``pytest -s`` from a real terminal and the import-time detection freezes
  ``color_system`` the other way.
* ``monkeypatch.setenv("NO_COLOR", "1")`` AFTER ``lode.cli`` is already
  imported is a silent no-op: the env read already happened at import, so
  such an assertion passes WITHOUT exercising the ``NO_COLOR`` path at all.

The only way to actually exercise ``NO_COLOR`` detection is a FRESH
SUBPROCESS that imports ``lode.cli`` with the target env already set, so the
shared ``console`` re-detects from scratch. This module is that pattern, and
it is proven non-vacuous by sabotaging the SUBJECT — ``src/lode/cli.py``'s
shared ``console`` — in both directions (re-verified against rich 15.0.0
during lode-xgaa's technical review):

* ``console = Console(no_color=False)`` (the Console stops honouring
  ``NO_COLOR``) makes ``test_console_no_color_true_when_env_set`` FAIL, while
  the control below still passes.
* ``console = Console(no_color=True)`` (colour wrongly forced off always)
  makes ``test_console_no_color_false_when_env_absent`` FAIL, while the
  positive test still passes.

Sabotaging the subject is the demonstration that counts. Breaking the
harness's own ``-c`` script fails the test too, but only circularly — it
shows nothing about whether these tests reach ``cli.py``'s console at all.
Any colour ticket (lode-l38d.4/.5/.6/.10) that asserts the ``NO_COLOR``
negative path should copy this pattern rather than reinvent — or silently
no-op — one of its own, and should re-run the two subject-sabotage checks
above rather than trust that a green test exercised anything.
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
    printed = result.stdout.strip()
    if printed not in ("True", "False"):
        # Never coerce unexpected output to False: that would let the
        # env-absent control below pass for the wrong reason, which is the
        # exact silent-pass failure mode this module exists to rule out.
        raise AssertionError(
            f"subprocess did not report a bare bool for console.no_color: {printed!r}"
        )
    return printed == "True"


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


def test_console_highlight_is_disabled() -> None:
    """``highlight=False`` (lode-re0s) is process-wide policy hoisted onto the
    shared ``console`` at construction, not left as a per-call-site kwarg —
    see ``src/lode/cli.py``'s ``console`` docstring for the ReprHighlighter
    defect this closes (a plain string like a rendered date gets shredded
    into mismatched styled spans by rich's default highlighter, verified
    against rich 15.0.0).

    Unlike ``no_color`` above, ``highlight`` is not environment-detected —
    it is a plain constructor kwarg — so this needs no subprocess; asserting
    it in-process exercises the real, already-imported shared console.

    rich exposes no public accessor for this flag, only the private
    ``Console._highlight`` — same reasoning as ``tests/test_cli_theme.py``
    pinning ``CLI_STYLES`` against the raw declaration rather than the
    merged-with-defaults ``Theme``.

    NON-VACUOUSNESS, demonstrated by sabotaging the subject: reverting
    ``cli.py``'s ``console = Console(theme=CLI_THEME, highlight=False)`` back
    to ``Console(theme=CLI_THEME)`` (its pre-lode-re0s form) makes this test
    FAIL (verified manually against the installed rich 15.0.0).
    """
    import lode.cli

    assert lode.cli.console._highlight is False


def test_err_console_highlight_is_disabled() -> None:
    """``highlight=False`` is a constructor kwarg on ``err_console`` too
    (lode-9jmv), mirroring the shared ``console`` above rather than being
    left as a per-call-site kwarg at its one ``print()`` call site — see
    ``src/lode/cli.py``'s ``console`` docstring, which explicitly warns that
    "IF A SECOND Console IS EVER ADDED to this module... it MUST also pass
    highlight=False". ``err_console`` is that second Console (lode-l810).

    Same reasoning as ``test_console_highlight_is_disabled`` above: no
    subprocess needed (``highlight`` is a plain constructor kwarg, not
    environment-detected), and the private ``Console._highlight`` is the
    only accessor rich exposes for this flag.

    NON-VACUOUSNESS, demonstrated by sabotaging the subject: reverting
    ``cli.py``'s ``err_console = Console(theme=CLI_THEME, stderr=True,
    highlight=False)`` back to ``Console(theme=CLI_THEME, stderr=True)``
    (its pre-lode-9jmv form) makes this test FAIL (verified manually
    against the installed rich 15.0.0).
    """
    import lode.cli

    assert lode.cli.err_console._highlight is False

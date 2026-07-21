"""Shared harness for driving the committed `PreToolUse(Bash)` guards in `.claude/settings.json`
*as actually shipped* -- never a reimplementation (lode-zlg8).

`test_bd_deps_guard.py` (lode-ij24), `test_gh_write_guard.py` (lode-o29m), and
`test_sha_fabrication_guard.py` (lode-fpmi) each independently defined a near-identical pair of
helpers -- one to pull a hook's shell one-liner out of the committed settings.json by matching a
substring, one to run it as a subprocess against a synthetic PreToolUse payload and unwrap the
`hookSpecificOutput`. Three copies of the harness contract (payload shape, exit-0 assertion,
`hookSpecificOutput` unwrap) meant every future change to it was an N-file edit, and let one copy
(`test_gh_write_guard.py`) silently drift onto `bash` instead of `/bin/sh` unnoticed -- see below.

CRITICAL (lode-9gm2): the Claude Code harness runs every PreToolUse hook under its `/bin/sh`,
which on Linux is **dash**, not bash -- and dash rejects bash-only constructs (`${var//pat/repl}`
pattern substitution, `$'...'` ANSI-C quoting) with a hard "Bad substitution" error that bricks
the Bash tool for the rest of the session (lode-m6px's first attempt at one of these guards
shipped exactly that, verified green by a test suite that drove the hook through `bash -c` and so
could not see the defect). `run_hook` below therefore always drives the hook through `/bin/sh -c`,
never `bash -c` -- that is the whole point: a test using bash cannot catch this class of bug,
which is exactly how it shipped once already, and exactly how `test_gh_write_guard.py`'s own
`shutil.which("bash")` call went unnoticed until this file existed to make the choice explicit and
shared (lode-zlg8).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"
SH = shutil.which("sh") or "/bin/sh"


def pretooluse_hook(match_substring: str) -> str:
    """The single `PreToolUse(Bash)` hook whose command contains `match_substring`, read from
    the committed `.claude/settings.json`. Fails loudly unless exactly one hook matches -- this
    is a test precondition, not a runtime guard, so an ambiguous or missing match is a bug in the
    test itself, not something to tolerate silently.
    """
    settings = json.loads(SETTINGS.read_text())
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matching = [
        h["command"]
        for entry in pre_tool_use
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
        if match_substring in h.get("command", "")
    ]
    assert len(matching) == 1, (
        f"expected exactly one hook matching {match_substring!r}, got {len(matching)}: {matching}"
    )
    return matching[0]


def run_hook(
    hook: str,
    command: str,
    *,
    path: str | None = None,
    cwd: Path | None = None,
) -> dict | None:
    """Run `hook` (a PreToolUse shell one-liner, as returned by `pretooluse_hook`) against a
    synthetic Bash-tool-call payload carrying `command`; return its `hookSpecificOutput`, or
    `None` if the hook fell through silently (no decision).

    Driven through **`/bin/sh -c`** (dash on Linux), never `bash -c` -- see the module docstring.

    `path`, when given, overrides `PATH` for the subprocess only -- used to simulate a jq-less
    machine (lode-oii9) without touching the real `PATH` of the process running the test. `sh`
    itself is invoked by its resolved path so a stripped `PATH` cannot make it unresolvable.

    `cwd`, when given, overrides the subprocess's working directory -- used by guards (like
    lode-fpmi's) whose logic depends on `git rev-parse --show-toplevel` resolving a real repo.
    """
    payload = json.dumps(
        {"session_id": "t", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    proc = subprocess.run(
        [SH, "-c", hook],
        input=payload,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env=None if path is None else {"PATH": path},
    )
    # A PreToolUse hook must always exit 0; a nonzero exit is itself a defect.
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]

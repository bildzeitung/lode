"""The GLOBAL 'which PreToolUse(Bash) guards are installed' inventory assertion.

This used to live inside `test_sha_fabrication_guard.py` (the newest of the three guards at the
time), which meant adding a fourth Bash guard anywhere in `.claude/settings.json` would turn an
unrelated file red for a reason that has nothing to do with SHA fabrication. It belongs once,
next to the shared hook-test harness (`tests/_hookharness.py`) all three guard suites import,
rather than inside whichever guard suite happened to be written most recently (lode-zlg8).
"""

from __future__ import annotations

import json
import shutil

import pytest

from _hookharness import SETTINGS

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the guards themselves shell out to jq"
)


def test_settings_json_still_carries_all_three_guards() -> None:
    """No regression to any of the three committed guards from adding a new one."""
    settings = json.loads(SETTINGS.read_text())
    bash_hooks = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
    ]
    assert any("blocks:" in c for c in bash_hooks), "lode-ij24 bd-deps guard missing"
    assert any("external-tracker" in c or "PENDING A HUMAN" in c for c in bash_hooks), (
        "lode-o29m gh-write guard missing"
    )
    assert any("sha-fabrication-guard.sh" in c for c in bash_hooks), (
        "lode-fpmi sha-fabrication guard missing"
    )
    assert len(bash_hooks) == 3

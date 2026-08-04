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

`SH_GLOB_DIRS` and `MD_GLOBS` (below) are the scan surface, chosen because between them
they are every place a `bd list` in this repo can actually be EXECUTED; each carries the
reason it is in scope, and a test pins that the globs really reach it. The one worth
arguing here is `.claude/agents/*.md`: a subagent definition is not a skill, but it
carries 20+ fenced-bash lines invoking `bd` (`rtk bd show`, `rtk bd update`, ...), so a
`bd list` added there is exactly as operative as one in a SKILL.md -- scoping this gate
to skills only would leave that whole surface unguarded, the same class of gap lode-200t
itself was filed about and the same widening `lode-lv04` made to a sibling gate.

Markdown mixes prose with executable content in two shapes, and BOTH are scanned: the
fenced blocks above, and inline single-backtick code spans inside a prose sentence (e.g.
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
never does: `tests/` is in neither glob, so that line is unreachable by construction --
checked and excluded deliberately, not by accident of scope. `docs/` is out for the same
reason plus one more: its only `bd list` mention
(`docs/agents-workflow.md:1502`) is a citation of `/sweep`'s query, and `docs/` is where
historical decision records deliberately quote pre-change commands.

## Blockquoted fences: the two paths normalize at different layers

`.claude/skills/code/SKILL.md` writes four of its nine executable bash blocks inside
markdown blockquotes (`> ```bash`). A `>` survives `.strip()`, so a scanner testing
`line.strip().startswith("```")` never opens the fence, and a `` ```...``` `` region
regex pairs the delimiters and removes the block from the inline scan too -- they were
once invisible to BOTH paths, the same shape of blind spot `lode-ovgs` records for
`tests/test_land_lock.py`'s column-0 `line.startswith`.

The FENCED path needs nothing from this file any more: `lode-wroz` moved the strip inside
the shared `tests/conftest.py::bash_fence_blocks`, which unmarks every line (delimiters
AND content), so every caller gets it -- and `test_skill_bash_state.py` now gates those
same four blocks directly. `lode-3pyo` therefore dropped the `_strip_blockquote`
pre-pass that used to run ahead of `_bash_blocks` here. That pre-pass was not merely
redundant: stripping twice is a no-op only on today's corpus, since a `>>`-leading line
loses one marker per pass (`>> log` would have reached this gate as `log`).

The INLINE path still normalizes its own input, and `_strip_blockquote` exists solely for
it: `inline_violations` never calls `_bash_blocks`, tracking fences itself line by line
(see its docstring for why), so without the strip a `> ```bash` fence is not a fence to
it and the block's contents get scanned as prose -- measured, one false positive on
`> echo "`bd list --json`"`. `test_inline_scan_skips_blockquoted_fenced_content` pins
exactly that. `_BLOCKQUOTE_MARKER` is one of THREE definitions both paths take from
conftest rather than re-declare -- the other two, `_FENCE_MARKER_RE` and `_closes_fence`,
fix where a fence opens and closes (lode-xqc7). A one-sided change to any of them would
make the two paths partition the same file differently, double-reporting fenced content
as prose.

## Why fenced/`.sh` comments are stripped but inline backtick spans are not

A `#`-comment inside a fenced block or a `.sh` file can never be executed, so a `bd list`
mention inside one is never an operative call site by construction -- stripping it (this
gate reuses `test_skill_bash_state.py`'s `_strip_comment`, the SAME logic that already
gates cross-block shell state in these same files, per lode-x495's coordination note)
removes real noise for free: `.claude/skills/sweep/SKILL.md`'s section-2 fenced block
carries a `# ... `bd list --json` rows already carry title...` comment that would
otherwise need its own skip entry. Markdown prose has no equivalent "this can never
run" structural signal -- an inline backtick span reads exactly like an operative command
whether it is one or not (contrast `.claude/skills/release/SKILL.md`'s real, executed
`` `bd list --status=closed --type=epic --limit 0` `` against
`.claude/skills/land/SKILL.md`'s `` `bd list --label needs-rebase --status in_progress` ``,
which is prose DESCRIBING `/code`'s real invocation -- already `--limit`-pinned at
`.claude/skills/code/SKILL.md:121` -- not a second site to pin here). That is exactly
why AC2 calls for an explicit, reasoned skip list rather than a cleverer regex: no
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

KNOWN LIMIT: the binary must be spelled literally. `"$BD" list` / `${BD} list` would not
match. Nothing in this repo invokes bd through a variable (every site is a literal `bd`
or `rtk bd`), and widening the regex to any word before `list` would drag in every
unrelated `... list` in the corpus -- so this stays a documented gap, not a fix.

## What counts as "has --limit": the SAME command, on the same line/span

`--limit` must appear in the same shell command as the `bd ... list` that needs it, not
merely somewhere on the same line -- `_command_segments` splits a line at unquoted `;`,
`|`, `&&`, `||` and `&` first, so
`bd list --label a --json; bd list --label b --limit 0 --json` correctly fails on its
first command instead of being excused by the second one's flag. Checking the whole raw
line was the obvious shape and it is a false NEGATIVE: this gate is the only thing that
will ever look at these sites, so a `--limit` that belongs to a different command must
not count.

Line CONTINUATIONS are still not followed: every real call site in this repo passes
`--limit` on the same physical line as `bd ... list` (verified against all current
sites, including the ones that pipe into `jq` via a trailing backslash -- `--limit 0`
always precedes it, never follows it). A future site that split `--limit` onto a
continuation line would be FLAGGED, not missed -- the safe direction -- and should be
rewritten to keep the flag on the invocation line rather than skip-listed.

Any explicit `--limit` satisfies this gate, not `--limit 0` specifically. That is
deliberate and matches AC1 ("passes an explicit `--limit`") and lode-2gun's own wording
("`--limit 0` **or an explicit, documented bound**"): the defect being guarded against
is bd applying an INVISIBLE default, and a written-out `--limit 50` is a visible choice
a reviewer sees in the diff. A site that wants unbounded reads still wants `--limit 0`;
this gate cannot tell intent apart from typo, and pretending otherwise would just make
it reject legitimate bounded queries.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# How the INLINE scan unmarks a blockquoted line, and where it starts and stops treating
# one as fenced. Imported, never re-declared: `bash_fence_blocks` applies these same three
# to the fenced path, so the two partition a document's FENCES identically by construction
# (module docstring: "Blockquoted fences").
from conftest import _BLOCKQUOTE_MARKER, _FENCE_MARKER_RE, _closes_fence

# Reuse lode-x495's fence-extraction and comment-stripping rather than adding a second,
# competing implementation of either -- this ticket's assertion (flag PRESENCE) is
# different from that one's (no cross-block variable use), but the underlying "what does
# an agent actually execute" parsing is identical, and lode-200t's own coordination note
# says to share it.
from test_skill_bash_state import _bash_blocks, _strip_comment

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# Every tracked `.sh` file in this repo lives directly under one of these two
# directories (pinned by `test_scan_scope_covers_every_tracked_sh_file`) -- the
# `.claude` half is lode-9bbq's scope correction: AC1's original scope
# (`.claude/skills/**/SKILL.md` + `scripts/*.sh`) excluded `.claude/statusline.sh`
# itself, the exact site that motivated this ticket.
SH_GLOB_DIRS = [REPO_ROOT / "scripts", REPO_ROOT / ".claude"]

# Markdown that a Claude Code agent executes bash out of: skills and subagent
# definitions alike (see the module docstring's scan-surface section).
MD_GLOBS = [(SKILLS_DIR, "*/SKILL.md"), (AGENTS_DIR, "*.md")]

# `bd`, then zero or more `-flag [value]` groups (tolerating a quoted or bare value),
# then a bare `list` token. See the module docstring's "The regex" section above for
# why this shape, and why `bd human list` / `bd dep list` correctly never match.
BD_LIST_RE = re.compile(
    r"\bbd\b"
    r'(?:\s+-{1,2}[A-Za-z][A-Za-z0-9-]*(?:=\S+)?(?:\s+(?:"[^"]*"|\'[^\']*\'|\S+))?)*'
    r"\s+list\b"
)

_LIMIT_RE = re.compile(r"--limit\b")

_INLINE_SPAN_RE = re.compile(r"`([^`\n]+)`")


# (repo-relative path, exact matched line text) -> reason a human can audit. Both maps
# below are NEGATIVE lists (known false positives), not the positive site inventory the
# ticket's reviewer warned against ("prefer a scanner that fails on a NEW unbounded call
# site, not one that pins a hand-maintained list of known-good sites") -- a new
# unguarded `bd ... list` anywhere in scope fails this gate by default; only an entry
# matching EXACTLY here is excused, and only for the text it names.
#
# They are split by CONTEXT on purpose. Every entry in SKIP_PROSE is justified by "this
# is prose describing a command, not a command this file runs" -- a claim about the
# surrounding markdown, not about the text itself. A single combined map would let that
# prose justification silently excuse the SAME text once somebody pastes it into a
# ```bash fence and it becomes operative, which is precisely the miss this gate exists
# to prevent (and is demonstrable: every entry below is an inline finding today).
# `test_no_skip_entry_is_stale` keeps both maps honest in the other direction -- an
# entry that stops matching anything is a failure, not dead weight that accumulates.

# Executed context: a fenced ```bash/```sh line, or a line of a `.sh` script.
SKIP_EXECUTED: dict[tuple[str, str], str] = {
    (
        "scripts/sweep-digest-id.sh",
        'echo "sweep-digest-id.sh: \\`bd list --label sweep-digest\\` failed" >&2',
    ): (
        "A diagnostic string for a FAILED bd invocation, quoting the command name for "
        "a human reading stderr -- not a second invocation. The real call two lines "
        'above (`if ! rows="$(bd list --label sweep-digest --all --limit 0 --json '
        '2>/dev/null)"; then`) already carries --limit 0.'
    ),
    (
        "scripts/sweep-digest-id.sh",
        'echo "sweep-digest-id.sh: could not parse \\`bd list\\` JSON" >&2',
    ): (
        "Same shape as the entry above -- a diagnostic string for a JSON-parse "
        "failure, quoting the command name, not a second invocation."
    ),
}

# Prose context: an inline single-backtick span outside any fence.
SKIP_PROSE: dict[tuple[str, str], str] = {
    (
        ".claude/skills/land/SKILL.md",
        "bd list --label needs-rebase --status in_progress",
    ): (
        "Prose describing /code's needs-rebase sweep, not a command this file "
        "executes -- the real, --limit-pinned invocation lives at "
        ".claude/skills/code/SKILL.md:121."
    ),
    (".claude/skills/land/SKILL.md", "bd list --label land-escalated"): (
        "Prose illustrating how a human resolves a land-escalated branch (`bd list "
        "--label land-escalated` reaching empty once every branch is resolved) -- "
        "not a command /land itself runs."
    ),
    (".claude/skills/release/SKILL.md", "bd list"): (
        "Prose describing bd list's general sort order ('bd list sorts "
        "priority-major, not by date') -- not an invocation. The real, "
        "--limit-pinned call two lines above it is the inline-backtick site this "
        "gate exists to catch (see the sabotage test for this exact site below)."
    ),
    (".claude/skills/sweep/SKILL.md", "bd list"): (
        "Three bare, generic mentions in this file's own explanatory prose ('--limit "
        "0 on every `bd list` in this skill', '...and the `-C` between `bd` and "
        "`list` is exactly what hid it from lode-2gun's literal `bd list` search', "
        "'--limit 0... so every `bd list` in this skill reads the same way') -- none "
        "is a literal invocation with its own arguments; every REAL `bd list` call "
        "in this file (sections 1, 2, 2a, 4) already carries --limit 0."
    ),
    (".claude/skills/sweep/SKILL.md", "bd list --help"): (
        "Cites bd's own --help text ('`bd list --help` documents --limit with a "
        "default of 50') as the source of the canonical --limit 0 rationale -- not "
        "an invocation this skill runs."
    ),
    (".claude/skills/sweep/SKILL.md", "bd list --status closed --json"): (
        "An illustrative example of a query returning hundreds of rows -- part of "
        "the canonical --limit 0 rationale's evidence, not a command this skill "
        "executes."
    ),
    (".claude/skills/sweep/SKILL.md", 'bd -C "$cwd" list'): (
        "Prose naming the exact statusline.sh shape that defeated lode-2gun's "
        "literal grep -- describing the site, not re-invoking it. The real site's "
        "own line is covered separately (see the sabotage test for "
        ".claude/statusline.sh below, and the SH-file scan of statusline.sh itself)."
    ),
}


def _strip_blockquote(markdown: str) -> str:
    """Drop one leading `> ` blockquote marker from every line. The INLINE scan's own
    normalization, and its only caller -- the fenced path gets this from `_bash_blocks`
    itself (module docstring: "Blockquoted fences")."""
    return "\n".join(_BLOCKQUOTE_MARKER.sub("", line) for line in markdown.splitlines())


def _command_segments(line: str) -> list[str]:
    """`line` split at unquoted shell command separators (`;`, `|`, `&`, and thereby
    `&&`/`||`), so a `--limit` can only excuse the `bd ... list` it actually belongs to.
    Quote tracking mirrors `_strip_comment`'s -- deliberately simple, no backslash
    escapes, sufficient for this corpus."""
    segments: list[str] = []
    start = 0
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in ";|&" and not in_single and not in_double:
            segments.append(line[start:i])
            start = i + 1
    segments.append(line[start:])
    return segments


def _is_unguarded(text: str) -> bool:
    """True if any single command within `text` runs `bd ... list` without `--limit`.

    Short-circuits before the per-character `_command_segments` split when `text`
    cannot contain a `bd` token at all: `BD_LIST_RE` requires a literal `bd`
    word-boundary token, so if the substring `"bd"` is absent, no segment could ever
    match it either -- `_command_segments` would only be splitting a line that has
    nothing to find. MEASURED (lode-vzj7): only 3.3% of scanned `.sh` lines contain the
    substring `bd` at all; guarding here cut `_scan_corpus` from 18.2ms to 10.7ms (-42%)
    with findings byte-identical to the unguarded version."""
    if "bd" not in text:
        return False
    return any(
        BD_LIST_RE.search(segment) and not _LIMIT_RE.search(segment)
        for segment in _command_segments(text)
    )


def _line_snippet(line: str) -> str | None:
    """The comment-stripped, trimmed line, if it carries an unguarded `bd ... list` --
    the shared check for fenced-block lines and whole `.sh` lines."""
    stripped = _strip_comment(line).strip()
    return stripped if _is_unguarded(stripped) else None


def sh_violations(text: str) -> list[tuple[str, int]]:
    """(snippet, 1-based line number) for every unguarded `bd ... list` line in a
    plain `.sh` script -- comments stripped, one line at a time."""
    found: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        snippet = _line_snippet(line)
        if snippet is not None:
            found.append((snippet, lineno))
    return found


def fenced_violations(markdown: str) -> list[tuple[str, int]]:
    """(snippet, line number) for every unguarded `bd ... list` inside a ```bash/```sh
    fence -- what an agent actually executes. `_bash_blocks` returns block TEXT with no
    line information, so the number is reported as -1 (rendered `?`).

    No `_strip_blockquote` pre-pass: `_bash_blocks` unmarks blockquoted lines itself
    (module docstring: "Blockquoted fences")."""
    found: list[tuple[str, int]] = []
    for block in _bash_blocks(markdown):
        for line in block.splitlines():
            snippet = _line_snippet(line)
            if snippet is not None:
                found.append((snippet, -1))
    return found


def inline_violations(markdown: str) -> list[tuple[str, int]]:
    """(span, 1-based line number) for every unguarded `bd ... list` in an inline
    single-backtick span OUTSIDE any fence.

    Fence tracking is a line-by-line state machine on `_FENCE_MARKER_RE` (imported from
    conftest -- see the import comment above) rather than a `` ```...``` `` region regex.
    Two reasons, both load-bearing: a region regex pairs delimiters by position, so a
    single stray ``` inside a block (`.claude/agents/coding.md:447` has one, in a
    comment) inverts every pairing after it and starts stripping PROSE instead of code --
    a silent false negative; and substituting the regions away destroys line numbers,
    which is not cosmetic here (the release/SKILL.md inline site really at line 129 was
    reported as line 96, sending a reader to the wrong place in the only message this
    gate ever prints).

    FINDING the fences is the same job `_bash_blocks` does, so both halves of it come
    from conftest -- `_FENCE_MARKER_RE` for where one opens, `_closes_fence` for where it
    closes -- and neither is re-implemented here, for the same reason `_strip_blockquote`
    shares `_BLOCKQUOTE_MARKER`: a one-sided divergence would make the two paths
    partition one document differently. What differs is what each does with the regions
    it found, which is why this stays a separate loop: `_bash_blocks` KEEPS only
    ```bash/```sh content, this one EXCLUDES every fence from the inline scan.

    One asymmetry survives that sharing, filed as lode-kjei: `_bash_blocks` opens only on
    a bash/sh info string, so it never tracks an ENCLOSING non-bash fence and reads a
    ```bash run nested inside a ````text block as executable, where this scan correctly
    reads the whole block as literal text. Latent -- zero nested fence openers exist
    across the repo's 58 markdown files, measured."""
    found: list[tuple[str, int]] = []
    fence = ""  # the opening run, e.g. "```" or "````" or "~~~"
    for lineno, line in enumerate(_strip_blockquote(markdown).splitlines(), 1):
        stripped = line.strip()
        if fence:
            if _closes_fence(stripped, fence):
                fence = ""
            continue
        m = _FENCE_MARKER_RE.match(stripped)
        if m:
            fence = m.group(1)
            continue
        for span in _INLINE_SPAN_RE.findall(line):
            if _is_unguarded(span):
                found.append((span, lineno))
    return found


# context -> the skip map that governs it. One lookup table, so `_apply_skips` below is
# the single place a skip is ever honoured -- the sabotage tests and the gate share it.
SKIPS = {"executed": SKIP_EXECUTED, "prose": SKIP_PROSE}

# (repo-relative path, snippet, line number, context).
Finding = tuple[str, str, int, str]


def scan_text(rel: str, text: str, *, markdown: bool) -> list[Finding]:
    """Every unguarded `bd ... list` in one file's text, BEFORE skip filtering."""
    if not markdown:
        return [(rel, s, n, "executed") for s, n in sh_violations(text)]
    return [(rel, s, n, "executed") for s, n in fenced_violations(text)] + [
        (rel, s, n, "prose") for s, n in inline_violations(text)
    ]


def _scan_corpus() -> list[Finding]:
    """`scan_text` over every file in scope."""
    findings: list[Finding] = []
    for base in SH_GLOB_DIRS:
        for path in sorted(base.glob("*.sh")):
            rel = str(path.relative_to(REPO_ROOT))
            findings += scan_text(rel, path.read_text(encoding="utf-8"), markdown=False)
    for base, pattern in MD_GLOBS:
        for path in sorted(base.glob(pattern)):
            rel = str(path.relative_to(REPO_ROOT))
            findings += scan_text(rel, path.read_text(encoding="utf-8"), markdown=True)
    return findings


def apply_skips(findings: list[Finding]) -> list[tuple[str, str, int]]:
    """(path, snippet, line number) for the findings no skip map excuses. A skip is
    honoured ONLY in the context it was written for -- see the two maps' shared comment
    above. `line number` is `-1` for a fenced-block finding, which has none."""
    return [
        (path, snippet, lineno)
        for path, snippet, lineno, context in findings
        if (path, snippet) not in SKIPS[context]
    ]


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
    assert (
        _line_snippet("rtk bd list --label foo --json")
        == "rtk bd list --label foo --json"
    )


def test_limit_from_a_different_command_does_not_excuse_this_one() -> None:
    """The false negative a whole-line `--limit` search has: two commands on one line,
    only the second one guarded. Each `bd ... list` must carry its own flag."""
    line = "rtk bd list --label a --json; rtk bd list --label b --limit 0 --json"
    assert _line_snippet(line) == line
    # ...and the mirror image, guarded command first.
    line = "rtk bd list --label a --limit 0 --json; rtk bd list --label b --json"
    assert _line_snippet(line) == line


def test_pipe_into_a_command_with_its_own_limit_flag_is_still_a_violation() -> None:
    assert _line_snippet("rtk bd list --json | somecmd --limit 5") is not None


def test_pipeline_after_a_guarded_bd_list_is_clean() -> None:
    """The real corpus shape -- `bd list ... --limit 0 --json | jq ...` -- must not be
    broken by the segment split."""
    assert (
        _line_snippet("rtk bd list --all --limit 0 --json | jq -r '.[] | .id'") is None
    )


def test_separator_inside_quotes_does_not_split_a_command() -> None:
    assert _line_snippet('rtk bd list --label "a;b" --limit 0 --json') is None


def test_comment_line_is_never_a_violation() -> None:
    """A `#`-comment can never execute -- mentioning `bd list` in one (as
    .claude/skills/sweep/SKILL.md's fenced section 2 does) must never need a
    skip-list entry."""
    assert _line_snippet("# `bd list --json` rows already carry title") is None


def test_fenced_block_without_limit_is_flagged() -> None:
    markdown = "```bash\nrtk bd list --label foo --json\n```\n"
    assert fenced_violations(markdown) == [("rtk bd list --label foo --json", -1)]


def test_fenced_block_with_limit_is_clean() -> None:
    markdown = "```bash\nrtk bd list --label foo --limit 0 --json\n```\n"
    assert fenced_violations(markdown) == []


def test_blockquoted_fence_is_still_extracted() -> None:
    """.claude/skills/code/SKILL.md writes four executable blocks as `> ```bash`.
    `_bash_blocks` unmarks them itself (lode-wroz), so nothing in THIS file is what
    makes this pass. Pinned here so a future change to the shared helper cannot
    silently take this gate's coverage with it."""
    markdown = "> ```bash\n> rtk bd list --label foo --json\n> ```\n"
    assert fenced_violations(markdown) == [("rtk bd list --label foo --json", -1)]


def test_indented_fence_is_still_extracted() -> None:
    """`_bash_blocks` tests `line.strip()`, so a fence indented under a markdown bullet
    is NOT the blind spot lode-ovgs records for tests/test_land_lock.py's column-0
    `line.startswith("```")`. Pinned here so a future change to the shared helper
    cannot silently take this gate's coverage with it."""
    markdown = "- bullet:\n\n  ```bash\n  rtk bd list --label foo --json\n  ```\n"
    assert fenced_violations(markdown) == [("rtk bd list --label foo --json", -1)]


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
    assert fenced_violations(markdown) == []


def test_inline_backtick_command_without_limit_is_flagged() -> None:
    """The release/SKILL.md shape: an operative command inside prose, never fenced."""
    markdown = "Run `bd list --status=closed --type=epic` for the window.\n"
    assert inline_violations(markdown) == [("bd list --status=closed --type=epic", 1)]


def test_inline_backtick_command_with_limit_is_clean() -> None:
    markdown = "Run `bd list --status=closed --type=epic --limit 0` for the window.\n"
    assert inline_violations(markdown) == []


def test_inline_scan_skips_fenced_content() -> None:
    """A single-backtick-looking sequence that is actually part of a fenced block's
    own content must be reported once (via the fenced path), never twice."""
    markdown = '```bash\necho "`bd list --json`"\n```\n'
    assert inline_violations(markdown) == []
    assert len(fenced_violations(markdown)) == 1


def test_inline_scan_skips_blockquoted_fenced_content() -> None:
    """The blockquoted twin of the test above, and the ONLY thing pinning
    `_strip_blockquote` since lode-3pyo took the fenced path off it -- deleting that call
    otherwise leaves the whole file green (sabotage-verified). This path tracks fences
    itself, so the strip is what makes `> ```bash` open one; without it the block's
    content is scanned as prose and falsely reported at line 2. The second assert is the
    real subject: both paths must partition the same document the same way, which is why
    they share one marker definition."""
    markdown = '> ```bash\n> echo "`bd list --json`"\n> ```\n'
    assert inline_violations(markdown) == []
    assert len(fenced_violations(markdown)) == 1


def test_inline_scan_agrees_with_fenced_scan_on_a_tilde_fence() -> None:
    """The tilde twin of `test_inline_scan_skips_blockquoted_fenced_content` (lode-xqc7).
    SABOTAGE-VERIFIED: under a bare `startswith("```")` toggle the `~~~` lines never open
    a fence at all, so the invocation is reported as prose at line 2."""
    markdown = '~~~bash\necho "`bd list --json`"\n~~~\n'
    assert inline_violations(markdown) == []
    assert len(fenced_violations(markdown)) == 1


def test_inline_scan_agrees_with_fenced_scan_on_a_four_backtick_fence() -> None:
    """The four-backtick twin (lode-xqc7). A bare ``` line -- shorter than the opening
    run -- is CONTENT under CommonMark's closing rule, not a close, which is the whole
    reason an author reaches for four backticks. SABOTAGE-VERIFIED: a bare
    `startswith("```")` toggle closes on that line, then reports the real invocation on
    the next one as prose at line 3."""
    markdown = '````bash\n```\necho "`bd list --json`"\n````\n'
    assert inline_violations(markdown) == []
    assert len(fenced_violations(markdown)) == 1


def test_inline_line_numbers_survive_a_preceding_fence() -> None:
    """A region-substitution fence stripper collapses the fence away and shifts every
    later line number (release/SKILL.md's line 129 was reported as 96). The state
    machine must report the real line."""
    markdown = "```bash\na\nb\nc\n```\nRun `bd list --json` here.\n"
    assert inline_violations(markdown) == [("bd list --json", 6)]


def test_stray_fence_delimiter_inside_a_block_does_not_invert_pairing() -> None:
    """.claude/agents/coding.md:447 carries a literal ```mermaid inside a fenced block,
    which a `` ```...``` `` region regex pairs with the NEXT real fence -- inverting
    every pairing after it, so prose gets stripped and real inline sites go unseen."""
    markdown = (
        "```bash\n"
        "echo 'parse every ```mermaid block'\n"
        "```\n"
        "Then run `bd list --json` to check.\n"
    )
    assert inline_violations(markdown) == [("bd list --json", 4)]


def test_non_bash_fence_is_never_scanned() -> None:
    markdown = "```text\nbd list --json\n```\n"
    assert fenced_violations(markdown) == []
    assert inline_violations(markdown) == []


def test_prose_without_backticks_is_never_scanned() -> None:
    """Deliberate scope limit (module docstring) -- un-marked-up prose mentioning
    `bd list` is never a candidate site, however it reads."""
    markdown = "bd list without any backticks at all is never executed here.\n"
    assert inline_violations(markdown) == []


# =====================================================================================
# The gate itself, against the real, shipped files.
# =====================================================================================


def test_every_skip_entry_is_live_and_justified() -> None:
    """Every skip entry must carry a reason AND still match a real finding, in the
    context its reason argues about. A hand-maintained exclusion list inside a drift
    detector is the one part of this gate that can rot silently: delete or reword a
    skipped line and the entry keeps excusing text that no longer exists, so the next
    reader inherits an exclusion nobody can audit. Failing loudly is the whole point --
    if this fires, DELETE the entry (or move it between the two maps), never widen
    it."""
    corpus = _scan_corpus()
    for context, skip_map in SKIPS.items():
        live = {(p, s) for p, s, _, ctx in corpus if ctx == context}
        stale = sorted(set(skip_map) - live)
        assert not stale, (
            f"stale {context} skip entries -- these no longer match anything in the "
            f"scanned corpus: {stale}"
        )
        for key, reason in skip_map.items():
            assert reason.strip(), f"skip entry {key} has an empty reason"


def test_a_prose_skip_never_excuses_a_fenced_invocation() -> None:
    """The reason every SKIP_PROSE entry gives is 'this is prose, not a command this
    file runs'. Pin that the justification is scoped to that context: paste the same
    text into a ```bash fence -- making it operative -- and the gate must flag it."""
    for rel, snippet in SKIP_PROSE:
        findings = scan_text(rel, f"```bash\n{snippet}\n```\n", markdown=True)
        assert apply_skips(findings), (
            f"prose skip {snippet!r} would excuse an operative fenced copy in {rel}"
        )


def test_scan_scope_covers_every_tracked_sh_file() -> None:
    """No tracked .sh file silently falls outside SH_GLOB_DIRS. If this ever fails, a
    new .sh file was added somewhere this gate's two globs don't reach -- widen
    SH_GLOB_DIRS, don't just let it fail quietly. The named assert is AC1's
    scope-widening correction: `.claude/statusline.sh` is the site lode-9bbq fixed and
    the reason this ticket exists, so it gets a failure message of its own rather than
    only showing up inside a set diff."""
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
    assert ".claude/statusline.sh" in scanned
    assert set(tracked) == scanned, set(tracked) - scanned


def test_scan_scope_covers_agent_definitions() -> None:
    """.claude/agents/*.md carries agent-executed fenced bash that invokes bd (20+
    lines across coding.md and code-reviewer.md today). A `bd list` added there is as
    operative as one in a SKILL.md, so the directory must be in the scan config AND the
    glob must actually be reaching those files -- not silently matching nothing."""
    assert (AGENTS_DIR, "*.md") in MD_GLOBS
    scanned = {
        str(p.relative_to(REPO_ROOT))
        for base, pattern in MD_GLOBS
        for p in base.glob(pattern)
    }
    assert ".claude/agents/coding.md" in scanned
    assert ".claude/agents/code-reviewer.md" in scanned
    bd_lines = sum(
        1
        for p in AGENTS_DIR.glob("*.md")
        for block in _bash_blocks(p.read_text(encoding="utf-8"))
        for line in block.splitlines()
        if re.search(r"\bbd\b", _strip_comment(line))
    )
    assert bd_lines > 0, "agent files stopped executing bd -- re-justify this scope"


def test_no_unguarded_bd_list_call_sites() -> None:
    """The actual gate. Every operative `bd ... list` call site in scope -- fenced
    bash/sh blocks and inline backtick spans in .claude/skills/*/SKILL.md and
    .claude/agents/*.md, plus every line of scripts/*.sh and .claude/*.sh -- must
    carry --limit, unless the exact (file, snippet) pair is skip-listed with a
    reason."""
    violations = apply_skips(_scan_corpus())
    assert not violations, "\n".join(
        f"{path}:{lineno if lineno != -1 else '?'}: unguarded `bd ... list` call "
        f"(no --limit): {snippet!r}. Either add --limit 0 (see "
        f".claude/skills/sweep/SKILL.md's canonical rationale), or -- only if this "
        f"is a confirmed non-operative mention -- add "
        f'("{path}", {snippet!r}) to SKIP_EXECUTED (fenced/.sh) or SKIP_PROSE '
        f"(inline backtick span) in tests/test_bd_list_limit_gate.py with a "
        f"specific reason."
        for path, snippet, lineno in violations
    )


# =====================================================================================
# Sabotage verification (lode-200t's acceptance bar): strip --limit 0 from REAL,
# currently-shipped sites and confirm the gate goes red.
# =====================================================================================


# (repo-relative path, the shipped `--limit 0` text, that text with the flag stripped).
# One case per EXTRACTION PATH, so a regression in any one of the three goes red here:
#   .claude/statusline.sh            -- .sh line scan; the exact site lode-9bbq fixed
#                                       and the reason this ticket exists.
#   scripts/epic-children-closed.sh  -- .sh line scan; the site whose sabotage-verified
#                                       gap lode-200t's own description opens with
#                                       (stripping --limit 0 here left every existing
#                                       test in the repo GREEN).
#   .claude/skills/land/SKILL.md     -- fenced ```bash block.
#   .claude/skills/release/SKILL.md  -- INLINE backtick span, never fenced: the site
#                                       that motivated AC1's "not fence-only" clause.
SABOTAGE_SITES = [
    (
        ".claude/statusline.sh",
        'bd -C "$cwd" list --limit 0 --json',
        'bd -C "$cwd" list --json',
    ),
    (
        "scripts/epic-children-closed.sh",
        'bd list --parent "$epic_id" --all --limit 0 --json',
        'bd list --parent "$epic_id" --all --json',
    ),
    (
        ".claude/skills/land/SKILL.md",
        "rtk bd list --label ready-for-land --status in_progress --limit 0 --json",
        "rtk bd list --label ready-for-land --status in_progress --json",
    ),
    (
        ".claude/skills/release/SKILL.md",
        "`bd list --status=closed --type=epic --limit 0`",
        "`bd list --status=closed --type=epic`",
    ),
]


@pytest.mark.parametrize(("rel", "shipped", "stripped"), SABOTAGE_SITES)
def test_sabotage_real_site_goes_red(rel: str, shipped: str, stripped: str) -> None:
    """Confirm the REAL file is clean today, then strip its `--limit 0` and confirm the
    gate flags exactly that. The clean-before assertion is what stops a vacuous pass,
    and the `!=` assertion is what catches a site that has since moved or been
    reworded (which would otherwise sabotage nothing and still pass)."""
    markdown = rel.endswith(".md")
    real_text = (REPO_ROOT / rel).read_text(encoding="utf-8")

    def scan(text: str) -> list[tuple[str, str, int]]:
        # The SAME scan + skip filter the gate itself runs, so a regression in the
        # production path cannot leave these green -- an earlier draft routed them
        # through text-only copies of the scanners, which could drift silently. The
        # skip filter matters too: these are real files carrying known prose false
        # positives, and only the NEW finding should show up.
        return apply_skips(scan_text(rel, text, markdown=markdown))

    assert scan(real_text) == [], (
        f"{rel} has an unexpected unguarded bd list site BEFORE sabotage -- "
        f"investigate before trusting the sabotage result"
    )

    sabotaged = real_text.replace(shipped, stripped)
    assert sabotaged != real_text, (
        f"sabotage replacement found no match in {rel} -- the site moved or was "
        f"reworded; update SABOTAGE_SITES rather than deleting the case"
    )
    assert any(stripped.strip("`") in snippet for _, snippet, _ in scan(sabotaged))
